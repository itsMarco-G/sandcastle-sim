"""Run a suite of eval cases against a live agent + stack.

Schema for an eval case (YAML):

    - name: light_off
      prompt: "turn off the kitchen counter light"
      expect:
        tool_calls:
          - name: turn_off
            args: { entity_id: light.kitchen_counter }
        final_state:
          light.kitchen_counter:
            state: "off"

Tool-call matching: each expected tool_calls entry must match at
least one actual call. `args` is a subset match (the agent can
pass extra args; missing required ones fail). Order-independent.

Final-state matching: each entity_id is read via get_device_state
after the prompt completes; the expected dict's keys must be
present and equal to the actual values. `state` is the top-level
HA state, anything else is looked up under `attributes`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..agent.one_shot import OneShotAgent, _mcp_to_ollama_tool


# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class ToolCallExpectation:
    """One expected tool call. ``args`` is a subset match — the
    actual call must have at least these key/value pairs."""

    name: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Expectation:
    """What the agent should do for one prompt.

    All fields are optional. An eval with no expectations passes
    trivially as long as the agent didn't error.
    """

    tool_calls: List[ToolCallExpectation] = field(default_factory=list)
    final_state: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class EvalCase:
    """One named prompt + the behavior we expect from the agent."""

    name: str
    prompt: str
    expect: Expectation = field(default_factory=Expectation)


@dataclass
class EvalResult:
    """Outcome of running one ``EvalCase``."""

    case: EvalCase
    passed: bool
    elapsed: float
    iterations: int
    tool_calls: List[Dict[str, Any]]
    failures: List[str] = field(default_factory=list)
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #


def load_suite(path: Path) -> List[EvalCase]:
    """Parse a YAML eval file into ``EvalCase``s.

    The file may be a top-level list of cases or a dict with a
    ``cases:`` key — both shapes are accepted so users can grow
    into adding metadata at the file level later.
    """
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("cases") or []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected list of cases, got {type(raw).__name__}")

    cases: List[EvalCase] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: case {i} is not a mapping")
        name = entry.get("name") or f"case_{i}"
        prompt = entry.get("prompt")
        if not prompt:
            raise ValueError(f"{path}: case {name!r} is missing 'prompt'")
        expect_raw = entry.get("expect") or {}
        expect = Expectation(
            tool_calls=[
                ToolCallExpectation(name=tc["name"], args=tc.get("args", {}))
                for tc in (expect_raw.get("tool_calls") or [])
            ],
            final_state=expect_raw.get("final_state") or {},
        )
        cases.append(EvalCase(name=name, prompt=prompt, expect=expect))
    return cases


# --------------------------------------------------------------------------- #
# Expectation matching                                                        #
# --------------------------------------------------------------------------- #


def _check_tool_calls(
    actual: List[Dict[str, Any]],
    expected: List[ToolCallExpectation],
) -> List[str]:
    """Return a list of failure messages (empty if all expected
    tool calls are satisfied)."""
    failures: List[str] = []
    for exp in expected:
        match = next(
            (
                a for a in actual
                if a.get("tool") == exp.name
                and _args_subset(exp.args, a.get("args") or {})
            ),
            None,
        )
        if match is None:
            args_repr = json.dumps(exp.args, sort_keys=True) if exp.args else "{}"
            actual_summary = ", ".join(
                f"{a.get('tool')}({json.dumps(a.get('args') or {}, sort_keys=True)})"
                for a in actual
            ) or "(none)"
            failures.append(
                f"expected tool call {exp.name}({args_repr}); "
                f"actual: {actual_summary}"
            )
    return failures


def _args_subset(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    """True if every k/v in `expected` is present in `actual`."""
    for k, v in expected.items():
        if k not in actual:
            return False
        if actual[k] != v:
            return False
    return True


async def _check_final_state(
    session: ClientSession,
    expected: Dict[str, Dict[str, Any]],
) -> List[str]:
    """For each expected entity, read its state and compare. Returns
    a list of failure messages (empty if everything matches)."""
    failures: List[str] = []
    for entity_id, fields in expected.items():
        try:
            result = await session.call_tool(
                "get_device_state", {"entity_id": entity_id},
            )
        except Exception as exc:
            failures.append(f"{entity_id}: get_device_state failed: {exc}")
            continue
        state = _unwrap(result)
        if "error" in state:
            failures.append(f"{entity_id}: {state['error']}")
            continue
        for key, want in fields.items():
            got = state.get(key) if key == "state" else (state.get("attributes") or {}).get(key)
            if got != want:
                failures.append(
                    f"{entity_id}.{key}: expected {want!r}, got {got!r}"
                )
    return failures


def _unwrap(call_result: Any) -> Dict[str, Any]:
    """Pull the JSON payload out of an MCP CallToolResult."""
    blocks = getattr(call_result, "content", None) or []
    if blocks:
        text = getattr(blocks[0], "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw": text}
    return {}


# --------------------------------------------------------------------------- #
# Suite execution                                                             #
# --------------------------------------------------------------------------- #


async def run_suite(
    cases: List[EvalCase],
    *,
    mcp_url: str = "http://localhost:8765/mcp/",
    ollama_url: str = "http://localhost:11434",
    model: str = "gemma4:e4b",
    max_iterations: int = 6,
    **agent_overrides: Any,
) -> List[EvalResult]:
    """Run every case once against a live MCP + Ollama stack.

    Opens the MCP session and the httpx client once and reuses them
    across all cases — so the model stays warm via keep_alive and
    the registry probe (``list_tools``) only happens once. Each case
    rebuilds the cheat sheet from current state since prior cases
    may have changed it.

    ``agent_overrides`` is forwarded to OneShotAgent — used for the
    CLI's ``--no-routing`` / ``--no-cheat-sheet`` flags so users can
    demo the diff harness without setting env vars that might leak
    into later invocations.
    """
    results: List[EvalResult] = []
    async with streamablehttp_client(mcp_url) as (read, write, _gs):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_list = await session.list_tools()
            all_tools = [_mcp_to_ollama_tool(t) for t in tool_list.tools]

            agent = OneShotAgent(
                mcp_url=mcp_url,
                ollama_url=ollama_url,
                model=model,
                max_iterations=max_iterations,
                quiet=True,        # don't print → tool_call lines during evals
                show_raw=False,
                stream=False,      # buffered reply, easier to handle in the loop
                # Pin the model for the whole suite so we don't pay
                # cold-load between cases.
                keep_alive=-1,
                **agent_overrides,
            )

            async with httpx.AsyncClient(timeout=300.0) as http:
                for case in cases:
                    results.append(
                        await _run_case(session, http, agent, all_tools, case)
                    )
    return results


async def _run_case(
    session: ClientSession,
    http: httpx.AsyncClient,
    agent: OneShotAgent,
    all_tools: List[Dict[str, Any]],
    case: EvalCase,
) -> EvalResult:
    """Run one prompt, capture tool calls + final state, score it."""
    started = time.monotonic()
    failures: List[str] = []
    error: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = []
    iterations = 0

    try:
        turn = await agent.run_turn(session, http, case.prompt, all_tools)
        tool_calls = turn.tool_calls
        iterations = turn.iterations
        if turn.error:
            error = turn.error
    except Exception as exc:
        error = f"agent crashed: {exc}"

    if error is None and case.expect.tool_calls:
        failures.extend(_check_tool_calls(tool_calls, case.expect.tool_calls))
    if error is None and case.expect.final_state:
        failures.extend(await _check_final_state(session, case.expect.final_state))

    elapsed = time.monotonic() - started
    passed = error is None and not failures
    return EvalResult(
        case=case,
        passed=passed,
        elapsed=elapsed,
        iterations=iterations,
        tool_calls=tool_calls,
        failures=failures,
        error=error,
    )


# --------------------------------------------------------------------------- #
# Reporting                                                                   #
# --------------------------------------------------------------------------- #


def print_report(results: List[EvalResult], *, suite_name: str = "") -> None:
    """Pretty-print the results as a Rich table-like list.

    Latency is first-class (per-case + a slowest line). No
    aggregate "score" framing — what passed and what didn't, with
    failure detail under each non-pass.
    """
    from rich.console import Console
    from rich.text import Text

    console = Console()
    title = f"Sandcastle Sim eval"
    if suite_name:
        title += f" — {suite_name}"
    console.rule(f"[bold cyan]{title}[/]", align="left")

    name_width = max((len(r.case.name) for r in results), default=10)

    for r in results:
        if r.error:
            mark = "[bold red]✗[/]"
            note = f"  [red]error: {r.error}[/]"
        elif r.passed:
            mark = "[bold green]✓[/]"
            note = ""
        else:
            mark = "[bold red]✗[/]"
            note = ""

        line = Text.from_markup(
            f"  {mark} [bold]{r.case.name:<{name_width}}[/]  "
            f"[dim]{r.elapsed:5.1f}s  {r.iterations} iter[/]"
        )
        console.print(line)
        if note:
            console.print(note)
        for f in r.failures:
            console.print(f"      [red]{f}[/]")

    console.print()
    n_pass = sum(1 for r in results if r.passed)
    total = len(results)
    elapsed_total = sum(r.elapsed for r in results)
    avg = elapsed_total / total if total else 0
    slowest = max(results, key=lambda r: r.elapsed) if results else None

    summary = (
        f"  {n_pass} of {total} cases produced expected behavior\n"
        f"  total {elapsed_total:.1f}s · per-case avg {avg:.1f}s"
    )
    if slowest:
        summary += f" · slowest {slowest.case.name} ({slowest.elapsed:.1f}s)"
    console.print(summary)


# --------------------------------------------------------------------------- #
# Persistence (baseline + diff)                                               #
# --------------------------------------------------------------------------- #
#
# A "baseline" is the JSON-serialised output of one eval run, saved
# to a known location. The diff workflow is:
#
#   1. Coding agent / developer runs `sandcastle-sim eval --save-baseline`
#      BEFORE making changes — this captures the current behavior.
#   2. They make changes to the agent / system prompt / topology / etc.
#   3. They run `sandcastle-sim eval --diff` — this re-runs the suite
#      and prints which cases regressed, which improved, and which
#      got noticeably slower or faster, with a non-zero exit code if
#      any regression appeared.
#
# Default baseline path is `<workdir>/.sandcastle/eval-baseline.json`.
# That's the same dir where PIDs and logs live, so the same .gitignore
# coverage applies.

# Latency change thresholds for "this got slower / faster" highlighting.
# We require BOTH a relative threshold (so 1ms -> 1.1ms doesn't fire)
# AND an absolute floor (so 0.5s -> 1.5s doesn't dwarf the report just
# because 200% sounds dramatic).
_LATENCY_DELTA_PCT = 0.20
_LATENCY_DELTA_ABS_S = 1.0


def default_baseline_path(workdir: Optional[Path] = None) -> Path:
    """Where the baseline lives by default."""
    workdir = workdir or Path.cwd()
    return workdir / ".sandcastle" / "eval-baseline.json"


def save_run(results: List[EvalResult], path: Path) -> None:
    """Serialise an eval run to JSON. Includes a timestamp so the
    diff can say "baseline saved 2 hours ago"."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results": [_result_to_dict(r) for r in results],
    }
    path.write_text(json.dumps(payload, indent=2))


def load_run(path: Path) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Read a saved eval run. Returns (saved_at, results-as-dicts).

    Returns (None, []) for a missing file so the caller can decide
    whether that's an error.
    """
    if not path.is_file():
        return None, []
    payload = json.loads(path.read_text())
    return payload.get("saved_at"), payload.get("results") or []


def _result_to_dict(r: EvalResult) -> Dict[str, Any]:
    """Flatten an EvalResult to a JSON-friendly dict."""
    return {
        "name": r.case.name,
        "prompt": r.case.prompt,
        "passed": r.passed,
        "elapsed": r.elapsed,
        "iterations": r.iterations,
        "tool_calls": r.tool_calls,
        "failures": r.failures,
        "error": r.error,
    }


@dataclass
class DiffEntry:
    name: str
    kind: str   # "regression" | "progression" | "unchanged_pass" | "unchanged_fail"
                # | "latency_regression" | "latency_progression" | "new" | "removed"
    baseline_passed: Optional[bool]
    current_passed: Optional[bool]
    baseline_elapsed: Optional[float]
    current_elapsed: Optional[float]
    failures: List[str]    # current run's failures, if any


def diff_runs(
    baseline: List[Dict[str, Any]],
    current: List[EvalResult],
) -> List[DiffEntry]:
    """Classify every case as regression / progression / unchanged / etc.

    Latency-only regressions (case still passes, but >20% AND >1s
    slower) get their own kind so the diff report can highlight a
    perf regression without confusing it with a behavioural one.
    """
    by_name_baseline = {r["name"]: r for r in baseline}
    by_name_current = {r.case.name: r for r in current}

    entries: List[DiffEntry] = []
    for name in by_name_current:
        cur = by_name_current[name]
        if name not in by_name_baseline:
            entries.append(DiffEntry(
                name=name,
                kind="new",
                baseline_passed=None,
                current_passed=cur.passed,
                baseline_elapsed=None,
                current_elapsed=cur.elapsed,
                failures=cur.failures,
            ))
            continue
        base = by_name_baseline[name]
        if base["passed"] and not cur.passed:
            kind = "regression"
        elif not base["passed"] and cur.passed:
            kind = "progression"
        elif base["passed"] and cur.passed:
            kind = _classify_latency(base["elapsed"], cur.elapsed)
        else:
            kind = "unchanged_fail"
        entries.append(DiffEntry(
            name=name,
            kind=kind,
            baseline_passed=base["passed"],
            current_passed=cur.passed,
            baseline_elapsed=base["elapsed"],
            current_elapsed=cur.elapsed,
            failures=cur.failures,
        ))
    for name in by_name_baseline:
        if name not in by_name_current:
            base = by_name_baseline[name]
            entries.append(DiffEntry(
                name=name,
                kind="removed",
                baseline_passed=base["passed"],
                current_passed=None,
                baseline_elapsed=base["elapsed"],
                current_elapsed=None,
                failures=[],
            ))
    return entries


def _classify_latency(base: float, cur: float) -> str:
    """Categorise a still-passing case by latency change."""
    if base <= 0:
        return "unchanged_pass"
    delta = cur - base
    pct = delta / base
    if pct > _LATENCY_DELTA_PCT and abs(delta) > _LATENCY_DELTA_ABS_S:
        return "latency_regression"
    if pct < -_LATENCY_DELTA_PCT and abs(delta) > _LATENCY_DELTA_ABS_S:
        return "latency_progression"
    return "unchanged_pass"


def has_regressions(entries: List[DiffEntry]) -> bool:
    """Did any case fail that previously passed, or noticeably slow down?"""
    return any(
        e.kind in ("regression", "latency_regression") for e in entries
    )


def print_diff_report(
    entries: List[DiffEntry],
    *,
    saved_at: Optional[str] = None,
    suite_name: str = "",
) -> None:
    """Pretty-print the diff with regressions highlighted first.

    Designed for two readers:

      1. A coding agent that needs to see at a glance "did my
         change break anything?" — regressions go first and get
         the loudest visual treatment.
      2. A human reviewing the same output — same priority order,
         readable failure detail.
    """
    from rich.console import Console
    from rich.text import Text

    console = Console()
    title = f"Sandcastle Sim eval — diff vs baseline"
    if suite_name:
        title += f" ({suite_name})"
    console.rule(f"[bold cyan]{title}[/]", align="left")

    if saved_at:
        console.print(f"  [dim]baseline saved at {saved_at}[/]")
    console.print()

    by_kind = {
        "regression":          [],
        "latency_regression":  [],
        "new":                 [],
        "removed":             [],
        "progression":         [],
        "latency_progression": [],
        "unchanged_pass":      [],
        "unchanged_fail":      [],
    }
    for e in entries:
        by_kind.setdefault(e.kind, []).append(e)

    name_width = max((len(e.name) for e in entries), default=10)

    def _fmt_latency(base: Optional[float], cur: Optional[float]) -> str:
        if base is None:
            return f"({cur:.1f}s)" if cur is not None else ""
        if cur is None:
            return f"(was {base:.1f}s)"
        delta = cur - base
        pct = (delta / base * 100) if base > 0 else 0
        sign = "+" if delta >= 0 else ""
        return f"({base:.1f}s → {cur:.1f}s, {sign}{pct:.0f}%)"

    if by_kind["regression"]:
        console.print(
            f"[bold red]REGRESSIONS ({len(by_kind['regression'])})[/] "
            f"[dim]— these were passing before:[/]"
        )
        for e in by_kind["regression"]:
            console.print(
                f"  [red]✗[/] [bold]{e.name:<{name_width}}[/]  "
                f"[red]PASS → FAIL[/]  "
                f"[dim]{_fmt_latency(e.baseline_elapsed, e.current_elapsed)}[/]"
            )
            for f in e.failures:
                console.print(f"      [red]{f}[/]")
        console.print()

    if by_kind["latency_regression"]:
        console.print(
            f"[bold yellow]LATENCY REGRESSIONS ({len(by_kind['latency_regression'])})[/] "
            f"[dim]— still passing, but noticeably slower:[/]"
        )
        for e in by_kind["latency_regression"]:
            console.print(
                f"  [yellow]⚠[/] [bold]{e.name:<{name_width}}[/]  "
                f"[dim]{_fmt_latency(e.baseline_elapsed, e.current_elapsed)}[/]"
            )
        console.print()

    if by_kind["new"]:
        console.print(
            f"[bold]NEW CASES ({len(by_kind['new'])})[/] "
            f"[dim]— added since baseline:[/]"
        )
        for e in by_kind["new"]:
            mark = "[green]✓[/]" if e.current_passed else "[red]✗[/]"
            console.print(
                f"  {mark} [bold]{e.name:<{name_width}}[/]  "
                f"[dim]{_fmt_latency(None, e.current_elapsed)}[/]"
            )
            if not e.current_passed:
                for f in e.failures:
                    console.print(f"      [red]{f}[/]")
        console.print()

    if by_kind["removed"]:
        console.print(
            f"[bold]REMOVED CASES ({len(by_kind['removed'])})[/] "
            f"[dim]— in baseline but not in current suite[/]"
        )
        for e in by_kind["removed"]:
            console.print(f"  [dim]· {e.name}[/]")
        console.print()

    if by_kind["progression"]:
        console.print(
            f"[bold green]PROGRESSIONS ({len(by_kind['progression'])})[/] "
            f"[dim]— newly passing:[/]"
        )
        for e in by_kind["progression"]:
            console.print(
                f"  [green]✓[/] [bold]{e.name:<{name_width}}[/]  "
                f"[green]FAIL → PASS[/]  "
                f"[dim]{_fmt_latency(e.baseline_elapsed, e.current_elapsed)}[/]"
            )
        console.print()

    if by_kind["latency_progression"]:
        console.print(
            f"[bold green]LATENCY IMPROVEMENTS ({len(by_kind['latency_progression'])})[/]"
        )
        for e in by_kind["latency_progression"]:
            console.print(
                f"  [green]↓[/] [bold]{e.name:<{name_width}}[/]  "
                f"[dim]{_fmt_latency(e.baseline_elapsed, e.current_elapsed)}[/]"
            )
        console.print()

    n_unchanged = len(by_kind["unchanged_pass"]) + len(by_kind["unchanged_fail"])
    if n_unchanged:
        console.print(
            f"[dim]UNCHANGED ({n_unchanged}): "
            f"{len(by_kind['unchanged_pass'])} passing, "
            f"{len(by_kind['unchanged_fail'])} still failing[/]"
        )
        console.print()

    n_reg = len(by_kind["regression"]) + len(by_kind["latency_regression"])
    n_prog = len(by_kind["progression"]) + len(by_kind["latency_progression"])
    if n_reg:
        console.print(
            f"[bold red]SUMMARY: {n_reg} regression(s), {n_prog} improvement(s). "
            f"ATTENTION REQUIRED.[/]"
        )
    elif n_prog:
        console.print(
            f"[bold green]SUMMARY: 0 regressions, {n_prog} improvement(s).[/]"
        )
    else:
        console.print(
            f"[dim]SUMMARY: 0 regressions, 0 improvements — behavior unchanged.[/]"
        )
