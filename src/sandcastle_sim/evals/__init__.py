"""Eval runner for Sandcastle Sim agents.

The eval suite is a regression net for *your* agent — write down
what it should do for a set of natural-language prompts ("turn off
the kitchen light" → fires turn_off(entity_id=light.kitchen_counter),
ends with that light off), then re-run those checks every time you
change the agent.

Not a leaderboard. Not a benchmark. The unit of value is "did my
change break something I cared about" — diff the report, not the
absolute numbers.

Public API:

* ``load_suite(path)`` — parse a YAML eval file into ``EvalCase``s
* ``run_suite(cases, ...)`` — execute the cases against a live MCP
  + Ollama stack, returning a list of ``EvalResult``
* ``print_report(results)`` — render the results as a Rich-styled
  per-case list with latency and failure detail
"""

from __future__ import annotations

from .runner import (
    DiffEntry,
    EvalCase,
    EvalResult,
    Expectation,
    ToolCallExpectation,
    default_baseline_path,
    diff_runs,
    has_regressions,
    load_run,
    load_suite,
    print_diff_report,
    print_report,
    run_suite,
    save_run,
)

__all__ = [
    "DiffEntry",
    "EvalCase",
    "EvalResult",
    "Expectation",
    "ToolCallExpectation",
    "default_baseline_path",
    "diff_runs",
    "has_regressions",
    "load_run",
    "load_suite",
    "print_diff_report",
    "print_report",
    "run_suite",
    "save_run",
]
