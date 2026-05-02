"""One-shot Ollama + MCP agent loop.

Usage from the CLI:

    sandcastle-sim "turn off the kitchen light"

The flow:

  1. Connect to the Sandcastle MCP server via streamable-HTTP and
     list its tools. Convert each MCP tool schema to the Ollama
     native tool shape.
  2. POST the user's prompt to Ollama's ``/api/chat`` with the tools.
  3. If the response message includes ``tool_calls``, dispatch each
     through MCP, append the tool results, and loop.
  4. When Ollama responds with plain text (no tool calls), print it.

Bounded by ``max_iterations`` (default 6) so a confused model
can't burn credits forever.

This module talks to Ollama's **native** ``/api/chat`` (not the
OpenAI-compat ``/v1/chat/completions`` shim). Native is the path
each model's chat template was actually trained for — tool-call
emission is more reliable, especially on smaller models like
``gemma4:e4b``. Trade-off: the request/response shape is
Ollama-specific, so swapping in OpenAI / Together / Groq means
swapping this module for an OpenAI-shaped one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Env-var defaults                                                            #
# --------------------------------------------------------------------------- #
#
# A few of the agent's optimizations (tool routing, cheat-sheet
# preload) can be turned off via env vars. This exists for two
# reasons:
#
# 1. Onboarding pedagogy — a new user can run `eval --save-baseline`
#    in the default optimized mode, then re-run with these vars set
#    and watch the diff harness catch the regression. Concrete proof
#    the optimizations matter.
# 2. Benchmarking the un-helped model. If you want to measure your
#    own agent's reasoning quality without our latency-saving
#    crutches, flip these off.
#
# Default behavior is unchanged — every flag still defaults to the
# fast / smart path when no env var is set.


def _env_truthy(name: str) -> bool:
    val = os.environ.get(name, "")
    return val.lower() not in ("", "0", "false", "no", "off")


def _route_tools_default() -> bool:
    """Disabled when SANDCASTLE_DISABLE_ROUTING is set.

    The README's eval-walkthrough uses this knob — it adds ~2-4 s
    per case on a Mac CPU (16 tool schemas instead of 4 in every
    prompt) without breaking any cases, so the diff lands cleanly
    as LATENCY REGRESSIONS without scary REGRESSIONS that might
    read as "did my install break."
    """
    return not _env_truthy("SANDCASTLE_DISABLE_ROUTING")


def _inject_cheat_sheet_default() -> bool:
    """Disabled when SANDCASTLE_DISABLE_CHEAT_SHEET is set.

    Bigger hit than disabling routing — the model has to call
    list_devices first, adding a whole LLM iteration. Use it for
    benchmarking the un-helped model rather than for onboarding
    demos.
    """
    return not _env_truthy("SANDCASTLE_DISABLE_CHEAT_SHEET")


# --------------------------------------------------------------------------- #
# ANSI styling                                                                #
# --------------------------------------------------------------------------- #
#
# Basic terminal colors / weights. Disabled when stdout isn't a TTY,
# when NO_COLOR is set (https://no-color.org/), or when a dumb terminal
# is detected. Codes are cached at import time; nothing fancier than
# ANSI escape sequences is used so we don't add a runtime dep.


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return sys.stdout.isatty() and sys.stderr.isatty()


_USE_COLOR = _color_enabled()


class _C:
    """ANSI escapes — empty strings when color is disabled."""

    RESET = "\033[0m" if _USE_COLOR else ""
    BOLD = "\033[1m" if _USE_COLOR else ""
    DIM = "\033[2m" if _USE_COLOR else ""
    ITAL = "\033[3m" if _USE_COLOR else ""
    UNDER = "\033[4m" if _USE_COLOR else ""
    RED = "\033[31m" if _USE_COLOR else ""
    GREEN = "\033[32m" if _USE_COLOR else ""
    YELLOW = "\033[33m" if _USE_COLOR else ""
    BLUE = "\033[34m" if _USE_COLOR else ""
    MAGENTA = "\033[35m" if _USE_COLOR else ""
    CYAN = "\033[36m" if _USE_COLOR else ""
    GRAY = "\033[90m" if _USE_COLOR else ""


SYSTEM_PROMPT_BASE = (
    "You are a smart-home assistant. Act on the user's request via the "
    "MCP tools. Tool selection: apply_scene for named scenes, "
    "apply_states for one-shot multi-device changes, direct tools "
    "(turn_on, turn_off, set_light, lock, ...) for single entities.\n\n"
    "RULES — follow exactly:\n"
    "1. No reasoning, analysis, or step-by-step planning. Emit the "
    "tool call immediately.\n"
    "2. No preamble like \"Sure, I'll...\" or \"Let me...\". Just call "
    "the tool.\n"
    "3. Use only entity_ids from the DEVICES list below. Never "
    "guess or invent one from the user's words.\n"
    "4. After the tool returns, reply in ONE short sentence "
    "confirming what changed. No explanations.\n"
)
"""Base prompt without the device cheat sheet — see _build_system_prompt."""


@dataclass
class OneShotResult:
    """What `run_one_shot` returns to the caller."""

    response_text: str
    iterations: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    response_already_printed: bool = False
    """True when the final reply was streamed live to stdout, so the
    caller should not print response_text again. Lets the CLI stay
    agnostic of streaming vs. non-streaming."""


@dataclass
class OneShotAgent:
    """Configuration + runner for a single Sandcastle agent turn."""

    mcp_url: str = "http://localhost:8765/mcp/"
    ollama_url: str = "http://localhost:11434"
    model: str = "gemma4:e4b"
    max_iterations: int = 6
    temperature: float = 0.0
    """0.0 is deterministic and slightly faster than 0.2 (no
    sampling overhead). The model still produces varied tool args
    when the input varies; we don't need creative variation here."""
    max_tokens: int = 256
    """Cap completion length. Most replies are <50 tokens visible
    text; tool calls fit in <100 tokens of args. 256 leaves headroom
    without letting a confused model run on for thousands of tokens."""
    keep_alive: Any = "30m"
    """How long Ollama keeps the model resident after the call.

    Accepts either a duration string ("30m", "5m", "1h") or an
    integer count of seconds. Use the integer ``-1`` (NOT the
    string ``"-1"`` — Ollama parses keep_alive strings as durations
    and rejects ``"-1"`` as malformed) to keep the model loaded
    indefinitely. ``0`` unloads immediately."""
    disable_thinking: bool = False
    """Pass `think: false` to /api/chat. Gemma's thinking-mode CoT
    pre-amble adds ~150 tokens of reasoning before the tool call,
    so this looks like an obvious latency win — but with thinking
    off, gemma4:e4b regresses to emitting tool calls as plain text
    ("turn_on(entity_id='light.kitchen_counter')") instead of
    structured tool_calls, breaking the agent loop. Off by default;
    flip on per-model after verifying it doesn't break tool emission."""
    inject_cheat_sheet: bool = field(default_factory=_inject_cheat_sheet_default)
    """Pre-fetch list_devices once and inline the result in the
    system prompt so the model never needs to call list_devices
    itself. Saves one full LLM iteration on most prompts. Default
    on; flip off via SANDCASTLE_DISABLE_CHEAT_SHEET=1 to benchmark
    the unassisted model."""
    route_tools: bool = field(default_factory=_route_tools_default)
    """Classify the user's prompt with a tiny keyword router and
    only send the relevant subset of MCP tools to the model. Cuts
    prompt tokens ~70% on typical onboarding requests — the
    difference between ~15 s and ~5 s per turn on a Mac CPU.
    Default on; flip off via SANDCASTLE_DISABLE_ROUTING=1 (the
    README's eval-walkthrough demo)."""
    quiet: bool = False
    """When False (default), print "(calling tool: name)" lines as they fire."""
    show_raw: bool = False
    """When True, dump the raw model message dict per iteration to
    stderr — content, tool_calls, and any thinking/reasoning fields
    Ollama returns. Useful for debugging why a prompt did or didn't
    trigger a tool call."""
    stream: bool = True
    """Stream tokens from Ollama as they arrive. The model's reply
    text appears live on stdout; tool_calls are accumulated across
    chunks and dispatched once the stream completes. Defaults on
    because watching tokens land is the cheapest possible "the
    model is alive" signal during a long generation."""

    async def run(self, user_message: str) -> OneShotResult:
        """Open MCP + httpx for one prompt and tear them down on return.

        For multi-prompt loops (the chat command) call ``run_turn``
        directly inside your own context managers so connections and
        the cached tool list survive across turns.
        """
        try:
            async with streamablehttp_client(self.mcp_url) as (read, write, _gs):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tool_list = await session.list_tools()
                    all_tools = [
                        _mcp_to_ollama_tool(t) for t in tool_list.tools
                    ]
                    async with httpx.AsyncClient(timeout=300.0) as http:
                        if not self.quiet:
                            await self._announce(session, all_tools, user_message)
                        return await self.run_turn(
                            session, http, user_message, all_tools,
                        )
        except httpx.ConnectError as exc:
            return OneShotResult(
                response_text="",
                error=(
                    f"Could not connect to Sandcastle MCP server at "
                    f"{self.mcp_url}: {exc}\n"
                    f"Is the server running? Start it with: sandcastle-sim mcp"
                ),
            )
        except Exception as exc:
            return OneShotResult(response_text="", error=f"Agent failed: {exc}")

    async def _announce(
        self,
        session: ClientSession,
        all_tools: List[Dict[str, Any]],
        user_message: str,
    ) -> None:
        """Print the connection / tool-routing summary line."""
        cheat_sheet = (
            await _build_cheat_sheet(session) if self.inject_cheat_sheet else ""
        )
        routed = (
            _route_tools(all_tools, user_message) if self.route_tools else all_tools
        )
        suffix = (
            f", cheat sheet preloaded ({cheat_sheet.count(chr(10))+1} lines)"
            if cheat_sheet else ""
        )
        routed_note = (
            f" (routed from {len(all_tools)})"
            if self.route_tools and len(routed) < len(all_tools) else ""
        )
        _info(
            f"connected to MCP — {len(routed)} tools{routed_note}{suffix}"
        )

    async def run_turn(
        self,
        session: ClientSession,
        http: httpx.AsyncClient,
        user_message: str,
        all_tools: List[Dict[str, Any]],
    ) -> OneShotResult:
        """Run one user-prompt -> reply turn against an open session.

        Builds a fresh system prompt (fresh cheat sheet + freshly
        routed tools) every turn so device-state changes from prior
        turns are reflected in the model's view of the home.
        """
        result = OneShotResult(response_text="")

        ollama_tools = (
            _route_tools(all_tools, user_message) if self.route_tools else all_tools
        )
        cheat_sheet = (
            await _build_cheat_sheet(session) if self.inject_cheat_sheet else ""
        )
        system_prompt = SYSTEM_PROMPT_BASE
        if cheat_sheet:
            system_prompt = SYSTEM_PROMPT_BASE + "\n" + cheat_sheet

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        for iteration in range(1, self.max_iterations + 1):
            result.iterations = iteration
            try:
                body: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "tools": ollama_tools,
                    "stream": self.stream,
                    "keep_alive": self.keep_alive,
                    "options": {
                        "temperature": self.temperature,
                        "num_predict": self.max_tokens,
                    },
                }
                if self.disable_thinking:
                    body["think"] = False
                url = f"{self.ollama_url.rstrip('/')}/api/chat"
                if self.stream:
                    payload = await _stream_with_progress(
                        http, url, body=body,
                        label=f"thinking (iter {iteration})",
                        quiet=self.quiet,
                    )
                else:
                    payload = await _post_with_progress(
                        http, url, body=body,
                        label=f"thinking (iter {iteration})",
                        quiet=self.quiet,
                    )
            except httpx.HTTPError as exc:
                detail = ""
                if isinstance(exc, httpx.HTTPStatusError):
                    detail = f" — {exc.response.text[:200]}"
                result.error = (
                    f"Ollama call failed: {exc}{detail}\n"
                    f"Is Ollama running at {self.ollama_url}? "
                    f"Try: `ollama serve` and `ollama pull {self.model}`."
                )
                return result

            msg = payload.get("message") or {}
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []

            if self.show_raw:
                _dump_raw(iteration, msg, payload)

            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                result.response_text = content.strip()
                result.response_already_printed = (
                    self.stream and bool(content)
                )
                return result

            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                args = fn.get("arguments")
                if isinstance(args, str):
                    args = _safe_json_loads(args)
                if not isinstance(args, dict):
                    args = {}
                if not name:
                    continue
                if not self.quiet:
                    _print_tool_call(name, args)
                try:
                    tool_result = await session.call_tool(name, args)
                    tool_content = _extract_text(tool_result)
                except Exception as exc:
                    tool_content = json.dumps({"error": str(exc)})
                result.tool_calls.append({
                    "tool": name,
                    "args": args,
                    "result": tool_content,
                })
                messages.append({
                    "role": "tool",
                    "content": tool_content,
                })

        result.error = (
            f"Hit max_iterations={self.max_iterations} without a final reply. "
            f"The model may be stuck in a tool-call loop; consider a stronger "
            f"model or revising the prompt."
        )
        return result


def run_one_shot(user_message: str, **kwargs: Any) -> OneShotResult:
    """Synchronous wrapper around ``OneShotAgent.run`` for CLI use."""
    agent = OneShotAgent(**kwargs)
    return asyncio.run(agent.run(user_message))


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


# Tool families used by the keyword router. Each maps a set of
# trigger phrases to a set of tool names. The router unions all
# matching families' tools so a prompt like "turn off the light and
# lock the door" gets both light and lock control surfaced.
#
# Tools that come along regardless of intent (state queries the
# model may want as a fallback) live in _ALWAYS_INCLUDED.
_TOOL_FAMILIES: List[tuple] = [
    # (trigger phrases, tool names)
    (
        ("turn on", "turn off", "switch on", "switch off",
         "light", "lamp", "dim", "brightness", "color", "colour"),
        ("turn_on", "turn_off", "set_light"),
    ),
    (
        ("lock", "unlock", "deadbolt", "door"),
        ("lock", "unlock"),
    ),
    (
        ("scene", "movie night", "cozy", "morning", "goodnight",
         "good night", "bedtime"),
        ("apply_scene", "apply_states"),
    ),
    (
        ("temperature", "thermostat", "heat", "cool", "ac ",
         "warmer", "colder", "hvac"),
        ("set_climate",),
    ),
    (
        ("blind", "shade", "curtain", "cover", "open the", "close the"),
        ("set_cover_position",),
    ),
    (
        ("vacuum", "clean", "robovac", "roomba", "hoover"),
        ("vacuum_control",),
    ),
    (
        ("happen", "recent", "event", "history", "earlier",
         "what just", "what was"),
        ("list_recent_events",),
    ),
    (
        ("save scene", "create scene", "store scene", "memorize"),
        ("save_scene",),
    ),
    (
        ("delete scene", "remove scene", "forget scene"),
        ("delete_scene",),
    ),
]
# get_device_state is cheap to ship and useful as a confirmation
# probe after any control action; the cheat sheet covers the
# pre-action read but not the post-action verify.
_ALWAYS_INCLUDED: tuple = ("get_device_state",)


def _route_tools(
    all_tools: List[Dict[str, Any]],
    user_message: str,
) -> List[Dict[str, Any]]:
    """Pick the subset of tool schemas the user prompt likely needs.

    Why bother: prompt-eval dominates latency on small CPU-served
    models. 16 tool schemas at ~200 tokens each = ~3000 prompt
    tokens. At 370 tok/s prompt-eval that's 8s gone before the
    model emits its first output token. Cutting to 3-4 relevant
    tools shaves 6-7s off cold turns.

    The router is intentionally dumb: lowercase keyword scan, union
    every family that matches. Falls back to the full tool set if
    nothing matches so out-of-vocabulary requests still work.
    """
    text = user_message.lower()
    keep: set = set(_ALWAYS_INCLUDED)
    for triggers, tool_names in _TOOL_FAMILIES:
        if any(trig in text for trig in triggers):
            keep.update(tool_names)
    if keep == set(_ALWAYS_INCLUDED):
        # No domain match — let the model see everything rather
        # than guess wrong.
        return all_tools
    routed = [
        t for t in all_tools
        if (t.get("function") or {}).get("name") in keep
    ]
    return routed or all_tools


def _mcp_to_ollama_tool(t: Any) -> Dict[str, Any]:
    """Convert an MCP Tool to Ollama's native tool shape.

    Ollama's ``/api/chat`` accepts the same {type, function: {name,
    description, parameters}} envelope as OpenAI, so the shape is
    identical to the OpenAI-compat layer's expectations. Kept as
    its own helper so callers can swap out the JSON-Schema pruning
    if a model gets confused by very large tool surfaces.
    """
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description or "",
            "parameters": t.inputSchema or {"type": "object"},
        },
    }


def _safe_json_loads(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_text(call_result: Any) -> str:
    """Pull the JSON-string payload out of an MCP CallToolResult."""
    blocks = getattr(call_result, "content", None) or []
    if blocks:
        text = getattr(blocks[0], "text", None)
        if text:
            return str(text)
    sc = getattr(call_result, "structuredContent", None)
    if sc is not None:
        try:
            return json.dumps(sc)
        except Exception:
            return str(sc)
    return ""


def _print_tool_call(name: str, args: Dict[str, Any]) -> None:
    args_str = json.dumps(args, separators=(",", ":"))
    if len(args_str) > 80:
        args_str = args_str[:77] + "..."
    print(
        f"  {_C.CYAN}→ {_C.BOLD}{name}{_C.RESET}{_C.CYAN}({args_str}){_C.RESET}",
        flush=True,
    )


def _info(msg: str) -> None:
    """Print a dimmed informational line to stderr."""
    sys.stderr.write(f"{_C.DIM}  · {msg}{_C.RESET}\n")
    sys.stderr.flush()


def _dump_raw(iteration: int, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Pretty-print the model's raw response for the given iteration.

    Goes to stderr so it doesn't get tangled with the user-facing
    answer on stdout. Includes content, tool_calls, and any extra
    fields Ollama returned (thinking, reasoning, eval timings).
    """
    sys.stderr.write(f"\n--- raw model output (iter {iteration}) ---\n")
    sys.stderr.write(json.dumps(message, indent=2, ensure_ascii=False))
    sys.stderr.write("\n")
    timings = {
        k: payload.get(k) for k in (
            "total_duration", "load_duration",
            "prompt_eval_count", "prompt_eval_duration",
            "eval_count", "eval_duration",
        ) if payload.get(k) is not None
    }
    if timings:
        ns_to_s = lambda ns: f"{ns / 1e9:.2f}s"
        formatted = {
            k: (ns_to_s(v) if k.endswith("_duration") else v)
            for k, v in timings.items()
        }
        sys.stderr.write(f"timings: {json.dumps(formatted)}\n")
    sys.stderr.write("--- end raw ---\n\n")
    sys.stderr.flush()


async def _stream_with_progress(
    http: httpx.AsyncClient,
    url: str,
    body: Dict[str, Any],
    label: str,
    quiet: bool,
) -> Dict[str, Any]:
    """Streaming variant of _post_with_progress.

    Uses Ollama's NDJSON streaming response: one JSON object per
    line, each containing partial ``message.content`` and possibly
    partial ``tool_calls``. We print content live to stdout as it
    arrives, accumulate tool_calls, and reconstruct the same dict
    shape the non-streaming path returns so the rest of the loop
    is oblivious to which mode it ran in.

    Progress UX:
      * Before the first content chunk arrives we tick "thinking
        (Ns)" on stderr so the cold-load wait isn't silent.
      * Once the first content chunk lands we clear the tick and
        switch to streaming raw tokens to stdout.
      * If the model's reply is purely a tool call (empty content),
        we keep the tick going until ``done=true`` arrives, then
        print a final timing line.
    """
    started = time.monotonic()
    tty = sys.stderr.isatty() and not quiet
    stop_tick = asyncio.Event()
    streaming_started = asyncio.Event()

    async def tick() -> None:
        try:
            await asyncio.wait_for(stop_tick.wait(), timeout=0.5)
            return
        except asyncio.TimeoutError:
            pass
        while not stop_tick.is_set():
            if streaming_started.is_set():
                # Once tokens are flowing, the spinner is noise.
                return
            elapsed = time.monotonic() - started
            sys.stderr.write(
                f"\r{_C.DIM}  · {label} ({elapsed:4.1f}s){_C.RESET}"
            )
            sys.stderr.flush()
            try:
                await asyncio.wait_for(stop_tick.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

    ticker = asyncio.create_task(tick()) if tty else None

    content_buf = ""
    tool_calls: List[Dict[str, Any]] = []
    final_payload: Dict[str, Any] = {}
    saw_content = False

    try:
        async with http.stream("POST", url, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = chunk.get("message") or {}
                delta = msg.get("content") or ""
                if delta:
                    if not saw_content:
                        # Clear the spinner row and start the
                        # answer on its own line.
                        if tty:
                            streaming_started.set()
                            sys.stderr.write("\r\033[K")
                            sys.stderr.flush()
                        saw_content = True
                    sys.stdout.write(delta)
                    sys.stdout.flush()
                    content_buf += delta
                # Tool calls may stream incrementally or arrive
                # whole in the final chunk; collect either way.
                tcs = msg.get("tool_calls")
                if tcs:
                    tool_calls.extend(tcs)
                if chunk.get("done"):
                    final_payload = chunk
                    break
    finally:
        stop_tick.set()
        if ticker is not None:
            try:
                await ticker
            except asyncio.CancelledError:
                pass
        # Always end the line cleanly.
        if saw_content:
            sys.stdout.write("\n")
            sys.stdout.flush()
        elif tty:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
        if not quiet:
            elapsed = time.monotonic() - started
            sys.stderr.write(
                f"{_C.DIM}  · {label} done ({elapsed:.1f}s){_C.RESET}\n"
            )
            sys.stderr.flush()

    # Reassemble the non-streaming response shape so the caller
    # can stay agnostic.
    final_payload["message"] = {
        "role": "assistant",
        "content": content_buf,
    }
    if tool_calls:
        final_payload["message"]["tool_calls"] = tool_calls
    return final_payload


async def _build_cheat_sheet(session: ClientSession) -> str:
    """Pre-fetch list_devices via MCP and format a compact cheat sheet.

    The Sandcastle CLI is an onboarding tool, not a general agent, so
    we know the topology is finite and stable across the session. By
    inlining entity_ids + states up-front we cut the typical
    "list_devices first, act second" flow from two LLM iterations to
    one — which on a Mac with cold gemma is the difference between
    ~30s and ~60s end-to-end.

    The cheat sheet is grouped by area, includes current state, and
    pulls scenes onto their own line. ~600-1500 tokens depending on
    the home; cheaper than the iteration it replaces.

    On any failure we silently return "" — the model can still call
    list_devices itself, just at the cost of an extra iteration.
    """
    try:
        result = await session.call_tool("list_devices", {})
    except Exception as exc:
        log.debug("cheat sheet fetch failed: %s", exc)
        return ""

    text = _extract_text(result)
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ""

    devices = data.get("devices") or []
    if not devices:
        return ""

    by_area: Dict[str, List[Dict[str, Any]]] = {}
    scenes: List[str] = []
    for d in devices:
        eid = d.get("entity_id", "")
        if eid.startswith("scene."):
            scenes.append(eid)
            continue
        area = d.get("area") or "(no area)"
        by_area.setdefault(area, []).append(d)

    lines = ["DEVICES (entity_id : state):"]
    for area in sorted(by_area):
        lines.append(f"[{area}]")
        for d in sorted(by_area[area], key=lambda x: x.get("entity_id", "")):
            eid = d.get("entity_id", "")
            state = d.get("state") or "?"
            lines.append(f"  {eid} : {state}")
    if scenes:
        lines.append("")
        lines.append(f"SCENES: {', '.join(sorted(scenes))}")
    return "\n".join(lines)


async def _post_with_progress(
    http: httpx.AsyncClient,
    url: str,
    body: Dict[str, Any],
    label: str,
    quiet: bool,
) -> Dict[str, Any]:
    """POST to Ollama, ticking elapsed seconds while we wait.

    The first call to a freshly-served Ollama can sit for 30-90s
    while the model loads into RAM/Metal — with no stdout output
    in the default flow it looks indistinguishable from a hang.
    A live elapsed-time tick on stderr proves we're alive without
    polluting stdout (where the model's reply will land).

    Falls back to a single non-animated line on non-TTY streams
    so logs don't fill with carriage returns.
    """
    started = time.monotonic()
    tty = sys.stderr.isatty() and not quiet
    stop = asyncio.Event()

    async def tick() -> None:
        # First update at 0.5 s — most warm calls finish before
        # then, so we don't flash a tick for those.
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
            return
        except asyncio.TimeoutError:
            pass
        while not stop.is_set():
            elapsed = time.monotonic() - started
            sys.stderr.write(
                f"\r{_C.DIM}  · {label} ({elapsed:4.1f}s){_C.RESET}"
            )
            sys.stderr.flush()
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

    if not tty:
        if not quiet:
            _info(f"{label} ...")
        resp = await http.post(url, json=body)
        resp.raise_for_status()
        if not quiet:
            elapsed = time.monotonic() - started
            _info(f"{label} done ({elapsed:.1f}s)")
        return resp.json()

    ticker = asyncio.create_task(tick())
    try:
        resp = await http.post(url, json=body)
        resp.raise_for_status()
        return resp.json()
    finally:
        stop.set()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        # Clear the spinner line and replace with a final summary.
        elapsed = time.monotonic() - started
        sys.stderr.write("\r\033[K")
        sys.stderr.write(
            f"{_C.DIM}  · {label} done ({elapsed:.1f}s){_C.RESET}\n"
        )
        sys.stderr.flush()
