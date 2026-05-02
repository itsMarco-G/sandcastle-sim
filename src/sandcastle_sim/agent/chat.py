"""Interactive chat REPL for the Sandcastle Sim CLI.

Wraps ``OneShotAgent.run_turn`` in a persistent loop: open MCP and
the Ollama HTTP client once, then read prompts from stdin and run
turns until the user types ``/quit`` or hits Ctrl-D.

Why a separate module from ``one_shot``: the per-turn iteration
logic is shared (factored as ``OneShotAgent.run_turn``), but the
session lifecycle is fundamentally different. One-shot opens and
closes connections per CLI invocation; chat keeps them open across
many turns so:

* The Ollama model stays warm in memory between prompts (no
  cold-load on the second prompt of a session).
* The MCP session stays subscribed to HA events so SSE state
  refresh stays current.
* The httpx client reuses its connection pool.

We also enable readline if available so the input prompt has line
editing and ↑↓ history.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, Dict, List, Optional

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .one_shot import (
    OneShotAgent,
    _build_cheat_sheet,
    _C,
    _info,
    _mcp_to_ollama_tool,
    _route_tools,
)


# One Rich console reused for all chat-side rendering. Force
# soft-wrap off so multi-line trees / panels keep their layout.
_console = Console(soft_wrap=False, highlight=False)

try:
    # readline plugs into builtin input(): arrow keys, history,
    # emacs/vi key bindings. Importing is enough to enable it.
    import readline  # noqa: F401
except ImportError:
    pass


_SAMPLE_PROMPTS: List[str] = [
    "turn off the kitchen counter light",
    "set up movie night",
    "lock the front door",
    "open the bedroom blinds",
    "what just happened in the home?",
    "dim the living lamp to 30%",
    "is the house secure?",
    "turn on the bedroom lights",
]

_SLASH_HELP = (
    "Slash commands:\n"
    "  /help              show this help + sample prompts\n"
    "  /devices           list every device + current state\n"
    "  /scenes            list available scenes\n"
    "  /events            recent events from Home Assistant\n"
    "  /raw [on|off]      toggle dumping raw model output\n"
    "  /clear             clear the screen\n"
    "  /quit  /exit  /q   leave the chat (Ctrl-D also works)"
)


def _print_banner(
    model: str,
    mcp_url: str,
    n_tools: int,
    n_devices: Optional[int] = None,
    n_areas: Optional[int] = None,
) -> None:
    """Render the chat banner as a Rich Panel.

    Two stacked sections inside the panel: the connection summary
    (model / MCP / home counts) and the prompt examples + slash
    hint. Counts default to None so a missing list_devices probe
    just hides the line gracefully.
    """
    body = Text()
    body.append("model: ", style="dim")
    body.append(f"{model}\n", style="bold")
    body.append("       (pinned in Ollama for this session)\n", style="dim")
    body.append("MCP:   ", style="dim")
    body.append(f"{mcp_url}", style="bold")
    body.append(f"  ({n_tools} tools)\n", style="dim")
    if n_devices is not None:
        areas = f" across {n_areas} areas" if n_areas else ""
        body.append("home:  ", style="dim")
        body.append(f"{n_devices} devices{areas}\n", style="bold")

    body.append("\n")
    body.append("Try:\n", style="bold")
    for prompt in _SAMPLE_PROMPTS:
        body.append(f"  {prompt}\n", style="dim")
    body.append("\nSlash: ", style="bold")
    body.append("/help  /devices  /scenes  /events  /clear  /quit\n", style="dim")
    body.append("(↑↓ recalls history; Ctrl-D also exits)", style="dim")

    _console.print(
        Panel(
            body,
            title="[bold cyan]Sandcastle Sim[/] — chat",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
            expand=False,
        )
    )


def _print_short_help() -> None:
    """Print /help output as Rich-styled sections."""
    samples = Text()
    for p in _SAMPLE_PROMPTS:
        samples.append(f"  {p}\n", style="dim")
    _console.print("\n[bold]Sample prompts:[/]")
    _console.print(samples, end="")
    _console.print("\n[bold]Slash commands:[/]")
    body = "\n".join(_SLASH_HELP.splitlines()[1:])
    _console.print(Text(body, style="dim"))


async def _count_topology(
    session: ClientSession,
) -> tuple[Optional[int], Optional[int]]:
    """Return (n_devices, n_areas) by hitting list_devices once.

    Best-effort: any failure returns (None, None) and the banner
    just doesn't show the home line. We avoid a separate
    list_areas call by deriving the area count from the device
    list — that matches what the cheat sheet shows the model.
    """
    try:
        result = await session.call_tool("list_devices", {})
    except Exception:
        return None, None
    blocks = getattr(result, "content", None) or []
    if not blocks:
        return None, None
    text = getattr(blocks[0], "text", "") or ""
    try:
        data = json.loads(text)
    except Exception:
        return None, None
    devices = data.get("devices") or []
    if not devices:
        return None, None
    areas = {d.get("area") for d in devices if d.get("area")}
    return len(devices), len(areas) or None


async def _warm_model(
    http: httpx.AsyncClient,
    ollama_url: str,
    model: str,
    keep_alive: str,
) -> float:
    """Send a tiny /api/chat to load the model into memory.

    Returns elapsed wall-clock seconds. Best-effort: if Ollama
    isn't reachable yet, swallow the error and let the first real
    prompt surface the problem.
    """
    started = time.monotonic()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ready"}],
        "stream": False,
        "keep_alive": keep_alive,
        # num_predict=1 short-circuits generation after one token
        # so the only real work is the model load itself.
        "options": {"num_predict": 1, "temperature": 0.0},
    }
    try:
        resp = await http.post(
            f"{ollama_url.rstrip('/')}/api/chat", json=body, timeout=300.0,
        )
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()[:300]
            raise RuntimeError(
                f"HTTP {resp.status_code} from Ollama: {detail}"
            )
    except Exception as exc:
        log_msg = (
            f"warm-up failed: {exc}. "
            f"First real prompt will pay the cold-load cost."
        )
        sys.stderr.write(f"{_C.YELLOW}  ! {log_msg}{_C.RESET}\n")
    return time.monotonic() - started


async def _warm_with_progress(
    http: httpx.AsyncClient, ollama_url: str, model: str, keep_alive: str,
) -> None:
    """Run _warm_model behind a "warming model ..." spinner."""
    started = time.monotonic()
    tty = sys.stderr.isatty()
    stop = asyncio.Event()
    label = f"warming {model}"

    async def tick() -> None:
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

    ticker = asyncio.create_task(tick()) if tty else None
    try:
        elapsed = await _warm_model(http, ollama_url, model, keep_alive)
    finally:
        stop.set()
        if ticker is not None:
            try:
                await ticker
            except asyncio.CancelledError:
                pass
    if tty:
        sys.stderr.write("\r\033[K")
    sys.stderr.write(
        f"{_C.GREEN}  ✓ {model} ready ({elapsed:.1f}s){_C.RESET}\n"
    )
    sys.stderr.flush()


async def _print_devices(session: ClientSession) -> None:
    """Render every device as a Rich Tree grouped by area."""
    try:
        result = await session.call_tool("list_devices", {})
    except Exception as exc:
        _console.print(f"[red](devices unavailable: {exc})[/]")
        return
    blocks = getattr(result, "content", None) or []
    if not blocks:
        _console.print("[dim](no devices reported)[/]")
        return
    text = getattr(blocks[0], "text", "") or ""
    try:
        data = json.loads(text)
    except Exception:
        _console.print(text)
        return
    devices = data.get("devices") or []
    if not devices:
        _console.print("[dim](no devices reported)[/]")
        return

    by_area: Dict[str, List[Dict[str, Any]]] = {}
    scenes: List[Dict[str, Any]] = []
    for d in devices:
        eid = d.get("entity_id") or ""
        if eid.startswith("scene."):
            scenes.append(d)
            continue
        area = d.get("area") or "(no area)"
        by_area.setdefault(area, []).append(d)

    tree = Tree(f"[bold]Home[/] ({len(devices)} devices)")
    for area in sorted(by_area):
        branch = tree.add(f"[bold cyan]{area}[/]")
        for d in sorted(by_area[area], key=lambda x: x.get("entity_id", "")):
            eid = d.get("entity_id", "")
            state = d.get("state") or "?"
            on = state in ("on", "open", "unlocked", "cleaning", "heat", "cool", "auto")
            state_style = "green" if on else "dim"
            branch.add(f"{eid}  [{state_style}]{state}[/]")
    if scenes:
        scene_branch = tree.add("[bold magenta]scenes[/]")
        for d in sorted(scenes, key=lambda x: x.get("entity_id", "")):
            scene_branch.add(d.get("entity_id", ""))
    _console.print(tree)


async def _print_scenes(session: ClientSession) -> None:
    """List scene IDs only."""
    try:
        result = await session.call_tool("list_devices", {"domain": "scene"})
    except Exception as exc:
        _console.print(f"[red](scenes unavailable: {exc})[/]")
        return
    blocks = getattr(result, "content", None) or []
    text = getattr(blocks[0], "text", "") if blocks else ""
    try:
        data = json.loads(text)
        scenes = data.get("devices") or []
    except Exception:
        _console.print("[dim](no scenes reported)[/]")
        return
    if not scenes:
        _console.print("[dim](no scenes reported)[/]")
        return
    _console.print("[bold]Scenes:[/]")
    for d in sorted(scenes, key=lambda x: x.get("entity_id", "")):
        _console.print(f"  [magenta]{d.get('entity_id', '')}[/]")


async def _print_events(session: ClientSession) -> None:
    """Render recent HA events as a Rich Table."""
    try:
        result = await session.call_tool("list_recent_events", {"limit": 20})
    except Exception as exc:
        _console.print(f"[red](events unavailable: {exc})[/]")
        return
    blocks = getattr(result, "content", None) or []
    if not blocks:
        _console.print("[dim](no recent events)[/]")
        return
    text = getattr(blocks[0], "text", "") or ""
    try:
        data = json.loads(text)
        events = data.get("events") or []
    except Exception:
        _console.print(text)
        return
    if not events:
        _console.print("[dim](no recent events)[/]")
        return
    table = Table(
        header_style="bold cyan", box=None, padding=(0, 1), expand=False,
    )
    table.add_column("when", style="dim")
    table.add_column("kind")
    table.add_column("entity", style="bold")
    table.add_column("state")
    for ev in events:
        ts = ev.get("when") or ev.get("timestamp") or ""
        kind = ev.get("kind") or ev.get("type") or ""
        eid = ev.get("entity_id") or ""
        state = ev.get("state") or ""
        table.add_row(str(ts), str(kind), str(eid), str(state))
    _console.print(table)


async def run_chat(agent: OneShotAgent) -> int:
    """Run the chat REPL until the user exits. Returns shell exit code."""
    # Pin the model in memory for the lifetime of the chat. With
    # the default 30m keep_alive, an idle pause longer than that
    # forces a cold reload on the next prompt — exactly the lag
    # we just spent four commits eliminating. -1 = never unload.
    # The pin persists in Ollama after we exit, so a follow-up
    # `sandcastle-sim chat` reuses the already-loaded model.
    # Note the integer: Ollama parses keep_alive strings as
    # durations, so "-1" is malformed; the int -1 is the sentinel.
    agent.keep_alive = -1
    try:
        async with streamablehttp_client(agent.mcp_url) as (read, write, _gs):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tool_list = await session.list_tools()
                all_tools = [_mcp_to_ollama_tool(t) for t in tool_list.tools]

                n_devices, n_areas = await _count_topology(session)
                _print_banner(
                    agent.model, agent.mcp_url, len(all_tools),
                    n_devices=n_devices, n_areas=n_areas,
                )

                async with httpx.AsyncClient(timeout=300.0) as http:
                    # Pre-load the model so the user's first real
                    # prompt doesn't pay the 30-90s cold-load tax.
                    await _warm_with_progress(
                        http, agent.ollama_url, agent.model, agent.keep_alive,
                    )

                    # ANSI codes inside the readline prompt confuse
                    # cursor accounting; \001..\002 marks them as
                    # zero-width so ↑↓ history recall lays out right.
                    if _C.RESET:
                        prompt_str = (
                            f"\n\001{_C.BOLD}{_C.GREEN}\002you > \001{_C.RESET}\002"
                        )
                    else:
                        prompt_str = "\nyou > "

                    while True:
                        try:
                            line = await asyncio.to_thread(input, prompt_str)
                        except (EOFError, KeyboardInterrupt):
                            print()
                            return 0
                        line = line.strip()
                        if not line:
                            continue

                        if line.startswith("/"):
                            cmd, _, arg = line[1:].partition(" ")
                            cmd = cmd.lower().strip()
                            arg = arg.strip()
                            if cmd in ("quit", "exit", "q"):
                                return 0
                            if cmd in ("help", "h", "?"):
                                _print_short_help()
                                continue
                            if cmd in ("clear", "cls"):
                                # ANSI clear; harmless on dumb terms.
                                sys.stdout.write("\033[2J\033[H")
                                sys.stdout.flush()
                                continue
                            if cmd == "devices":
                                await _print_devices(session)
                                continue
                            if cmd == "scenes":
                                await _print_scenes(session)
                                continue
                            if cmd == "events":
                                await _print_events(session)
                                continue
                            if cmd == "raw":
                                if arg in ("on", "true", "1", ""):
                                    agent.show_raw = not agent.show_raw if not arg else True
                                elif arg in ("off", "false", "0"):
                                    agent.show_raw = False
                                state = "on" if agent.show_raw else "off"
                                print(f"raw output: {state}")
                                continue
                            print(
                                f"{_C.YELLOW}unknown command:{_C.RESET} "
                                f"/{cmd}  {_C.GRAY}(try /help){_C.RESET}"
                            )
                            continue

                        # Live turn — stream tokens straight to stdout.
                        result = await agent.run_turn(
                            session, http, line, all_tools,
                        )
                        if result.error:
                            sys.stderr.write(
                                f"\n{_C.RED}error:{_C.RESET} {result.error}\n"
                            )
                            continue
                        if not result.response_already_printed and result.response_text:
                            print(result.response_text)
    except httpx.ConnectError as exc:
        print(
            f"{_C.RED}Could not connect to MCP at {agent.mcp_url}:{_C.RESET} {exc}\n"
            f"Start it with: sandcastle-sim mcp",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"{_C.RED}chat failed:{_C.RESET} {exc}", file=sys.stderr)
        return 1


def run_chat_sync(**kwargs: Any) -> int:
    """Synchronous wrapper around :func:`run_chat` for the CLI."""
    agent = OneShotAgent(**kwargs)
    try:
        return asyncio.run(run_chat(agent))
    except KeyboardInterrupt:
        return 0
