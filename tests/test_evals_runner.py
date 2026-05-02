"""Unit tests for the eval-suite loader + expectation matchers.

These run without a live stack. The end-to-end eval execution
(running real cases against MCP + Ollama) is intentionally not
in CI — evals are a developer-side regression net, run manually
or on a self-hosted runner with a GPU.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from sandcastle_sim.evals.runner import (
    EvalCase,
    EvalResult,
    Expectation,
    ToolCallExpectation,
    _args_subset,
    _check_tool_calls,
    diff_runs,
    has_regressions,
    load_run,
    load_suite,
    save_run,
)


# --------------------------------------------------------------------------- #
# Loader                                                                      #
# --------------------------------------------------------------------------- #


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "suite.yaml"
    p.write_text(text)
    return p


def test_load_minimal_case(tmp_path: Path):
    cases = load_suite(_write(tmp_path, """\
- name: hello
  prompt: turn off the kitchen light
"""))
    assert len(cases) == 1
    assert cases[0].name == "hello"
    assert cases[0].prompt == "turn off the kitchen light"
    assert cases[0].expect.tool_calls == []
    assert cases[0].expect.final_state == {}


def test_load_full_case(tmp_path: Path):
    cases = load_suite(_write(tmp_path, """\
- name: light_off
  prompt: "turn off the kitchen counter light"
  expect:
    tool_calls:
      - name: turn_off
        args: { entity_id: light.kitchen_counter }
    final_state:
      light.kitchen_counter:
        state: "off"
"""))
    assert len(cases) == 1
    case = cases[0]
    assert case.expect.tool_calls[0].name == "turn_off"
    assert case.expect.tool_calls[0].args == {"entity_id": "light.kitchen_counter"}
    assert case.expect.final_state == {"light.kitchen_counter": {"state": "off"}}


def test_load_dict_form_with_cases_key(tmp_path: Path):
    """Allow file-level metadata growth via a top-level mapping."""
    cases = load_suite(_write(tmp_path, """\
description: smoke
cases:
  - name: foo
    prompt: bar
"""))
    assert len(cases) == 1
    assert cases[0].name == "foo"


def test_load_missing_prompt_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="prompt"):
        load_suite(_write(tmp_path, """\
- name: bad
"""))


def test_load_empty_file_returns_no_cases(tmp_path: Path):
    assert load_suite(_write(tmp_path, "")) == []


# --------------------------------------------------------------------------- #
# Args subset matcher                                                         #
# --------------------------------------------------------------------------- #


def test_args_subset_exact_match():
    assert _args_subset({"a": 1}, {"a": 1}) is True


def test_args_subset_extra_actual_args_ok():
    """Agent passing extra args shouldn't fail expectation match."""
    assert _args_subset({"a": 1}, {"a": 1, "b": 2}) is True


def test_args_subset_missing_expected_arg():
    assert _args_subset({"a": 1, "b": 2}, {"a": 1}) is False


def test_args_subset_value_mismatch():
    assert _args_subset({"a": 1}, {"a": 2}) is False


def test_args_subset_empty_expected_always_ok():
    """No expected args means any actual args satisfy."""
    assert _args_subset({}, {"a": 1, "b": 2}) is True


# --------------------------------------------------------------------------- #
# Tool-call matcher                                                           #
# --------------------------------------------------------------------------- #


def _tc(name: str, **args: Any) -> Dict[str, Any]:
    return {"tool": name, "args": args, "result": ""}


def test_tool_calls_satisfied_in_order():
    failures = _check_tool_calls(
        actual=[_tc("turn_off", entity_id="light.kitchen_counter")],
        expected=[
            ToolCallExpectation(
                name="turn_off",
                args={"entity_id": "light.kitchen_counter"},
            ),
        ],
    )
    assert failures == []


def test_tool_calls_satisfied_out_of_order():
    """Order doesn't matter — agent can use list_devices first."""
    failures = _check_tool_calls(
        actual=[
            _tc("list_devices"),
            _tc("turn_off", entity_id="light.kitchen_counter"),
        ],
        expected=[
            ToolCallExpectation(
                name="turn_off",
                args={"entity_id": "light.kitchen_counter"},
            ),
        ],
    )
    assert failures == []


def test_tool_calls_wrong_entity_fails():
    failures = _check_tool_calls(
        actual=[_tc("turn_off", entity_id="light.bedroom_main")],
        expected=[
            ToolCallExpectation(
                name="turn_off",
                args={"entity_id": "light.kitchen_counter"},
            ),
        ],
    )
    assert len(failures) == 1
    assert "turn_off" in failures[0]
    assert "light.kitchen_counter" in failures[0]


def test_tool_calls_missing_entirely_fails():
    failures = _check_tool_calls(
        actual=[],
        expected=[ToolCallExpectation(name="turn_off")],
    )
    assert len(failures) == 1


# --------------------------------------------------------------------------- #
# Persistence + diff (the coding-agent regression workflow)                   #
# --------------------------------------------------------------------------- #


def _make_result(
    name: str, *, passed: bool = True, elapsed: float = 1.0,
    failures: List[str] | None = None,
) -> EvalResult:
    return EvalResult(
        case=EvalCase(name=name, prompt="x"),
        passed=passed,
        elapsed=elapsed,
        iterations=1,
        tool_calls=[],
        failures=failures or [],
        error=None,
    )


def test_save_then_load_round_trip(tmp_path: Path):
    path = tmp_path / "baseline.json"
    save_run([_make_result("a"), _make_result("b", passed=False)], path)
    saved_at, results = load_run(path)
    assert saved_at is not None
    assert {r["name"] for r in results} == {"a", "b"}
    assert next(r for r in results if r["name"] == "a")["passed"] is True
    assert next(r for r in results if r["name"] == "b")["passed"] is False


def test_load_missing_baseline_returns_empty(tmp_path: Path):
    saved_at, results = load_run(tmp_path / "nope.json")
    assert saved_at is None
    assert results == []


def test_diff_detects_regression(tmp_path: Path):
    """A case that was passing and is now failing must be flagged."""
    baseline_path = tmp_path / "b.json"
    save_run([_make_result("light_off", passed=True, elapsed=3.0)], baseline_path)
    _, baseline = load_run(baseline_path)

    current = [_make_result("light_off", passed=False, elapsed=4.0,
                             failures=["expected turn_off, got turn_on"])]
    entries = diff_runs(baseline, current)
    assert len(entries) == 1
    assert entries[0].kind == "regression"
    assert has_regressions(entries) is True


def test_diff_detects_progression(tmp_path: Path):
    baseline_path = tmp_path / "b.json"
    save_run([_make_result("foo", passed=False)], baseline_path)
    _, baseline = load_run(baseline_path)

    current = [_make_result("foo", passed=True)]
    entries = diff_runs(baseline, current)
    assert entries[0].kind == "progression"
    assert has_regressions(entries) is False


def test_diff_unchanged_within_latency_threshold(tmp_path: Path):
    """Small timing wobbles should NOT show as regressions."""
    baseline_path = tmp_path / "b.json"
    save_run([_make_result("x", passed=True, elapsed=3.0)], baseline_path)
    _, baseline = load_run(baseline_path)

    current = [_make_result("x", passed=True, elapsed=3.2)]  # +6%
    entries = diff_runs(baseline, current)
    assert entries[0].kind == "unchanged_pass"
    assert has_regressions(entries) is False


def test_diff_flags_significant_latency_regression(tmp_path: Path):
    """Still passing but >20% AND >1s slower → latency regression."""
    baseline_path = tmp_path / "b.json"
    save_run([_make_result("x", passed=True, elapsed=3.0)], baseline_path)
    _, baseline = load_run(baseline_path)

    current = [_make_result("x", passed=True, elapsed=5.0)]  # +66%, +2s
    entries = diff_runs(baseline, current)
    assert entries[0].kind == "latency_regression"
    assert has_regressions(entries) is True


def test_diff_handles_new_and_removed_cases(tmp_path: Path):
    baseline_path = tmp_path / "b.json"
    save_run([_make_result("kept"), _make_result("dropped")], baseline_path)
    _, baseline = load_run(baseline_path)

    current = [_make_result("kept"), _make_result("added")]
    entries = diff_runs(baseline, current)
    by_name = {e.name: e.kind for e in entries}
    assert by_name["kept"] == "unchanged_pass"
    assert by_name["added"] == "new"
    assert by_name["dropped"] == "removed"


# --------------------------------------------------------------------------- #
# Env-var-driven agent defaults (the pedagogy knob for the README walkthrough) #
# --------------------------------------------------------------------------- #


def test_default_agent_has_optimizations_on(monkeypatch):
    """No env vars set → routing + cheat sheet on."""
    monkeypatch.delenv("SANDCASTLE_DISABLE_ROUTING", raising=False)
    monkeypatch.delenv("SANDCASTLE_DISABLE_CHEAT_SHEET", raising=False)
    from sandcastle_sim.agent.one_shot import OneShotAgent
    agent = OneShotAgent()
    assert agent.route_tools is True
    assert agent.inject_cheat_sheet is True


def test_disable_routing_env_only_affects_routing(monkeypatch):
    """The README walkthrough's demo knob — milder regression.

    Disabling routing alone slows each case but keeps every case
    passing, so the diff lands as clean LATENCY REGRESSIONS.
    """
    monkeypatch.setenv("SANDCASTLE_DISABLE_ROUTING", "1")
    monkeypatch.delenv("SANDCASTLE_DISABLE_CHEAT_SHEET", raising=False)
    from sandcastle_sim.agent.one_shot import OneShotAgent
    agent = OneShotAgent()
    assert agent.route_tools is False
    assert agent.inject_cheat_sheet is True


def test_disable_cheat_sheet_env_only_affects_cheat_sheet(monkeypatch):
    monkeypatch.delenv("SANDCASTLE_DISABLE_ROUTING", raising=False)
    monkeypatch.setenv("SANDCASTLE_DISABLE_CHEAT_SHEET", "1")
    from sandcastle_sim.agent.one_shot import OneShotAgent
    agent = OneShotAgent()
    assert agent.route_tools is True
    assert agent.inject_cheat_sheet is False


def test_both_env_vars_compose(monkeypatch):
    """Setting both granular vars disables both optimizations."""
    monkeypatch.setenv("SANDCASTLE_DISABLE_ROUTING", "1")
    monkeypatch.setenv("SANDCASTLE_DISABLE_CHEAT_SHEET", "1")
    from sandcastle_sim.agent.one_shot import OneShotAgent
    agent = OneShotAgent()
    assert agent.route_tools is False
    assert agent.inject_cheat_sheet is False


def test_explicit_kwargs_override_env(monkeypatch):
    """Caller-passed kwargs always win over env defaults.

    This is what the CLI's --no-routing / --no-cheat-sheet flags
    rely on: per-invocation overrides that can't leak between
    runs the way an `export SANDCASTLE_DISABLE_ROUTING=1` would."""
    monkeypatch.setenv("SANDCASTLE_DISABLE_ROUTING", "1")
    monkeypatch.setenv("SANDCASTLE_DISABLE_CHEAT_SHEET", "1")
    from sandcastle_sim.agent.one_shot import OneShotAgent
    agent = OneShotAgent(route_tools=True, inject_cheat_sheet=True)
    assert agent.route_tools is True
    assert agent.inject_cheat_sheet is True


def test_cli_optimization_overrides_helper(monkeypatch):
    """The _optimization_overrides helper translates CLI flags
    into OneShotAgent kwargs without touching env vars — so
    --no-routing for one command can't leak into the next."""
    import argparse

    from sandcastle_sim.cli import _optimization_overrides

    args = argparse.Namespace(no_routing=True, no_cheat_sheet=False)
    assert _optimization_overrides(args) == {"route_tools": False}

    args = argparse.Namespace(no_routing=False, no_cheat_sheet=True)
    assert _optimization_overrides(args) == {"inject_cheat_sheet": False}

    args = argparse.Namespace(no_routing=True, no_cheat_sheet=True)
    assert _optimization_overrides(args) == {
        "route_tools": False,
        "inject_cheat_sheet": False,
    }

    args = argparse.Namespace(no_routing=False, no_cheat_sheet=False)
    assert _optimization_overrides(args) == {}


def test_bundled_quick_yaml_loads():
    """The quick suite ships with the package — it must always
    parse so a fresh `pip install` user can `eval` immediately."""
    from importlib import resources
    bundled = resources.files("sandcastle_sim").joinpath(
        "data", "evals", "quick.yaml",
    )
    cases = load_suite(Path(str(bundled)))
    assert len(cases) >= 5
    names = {c.name for c in cases}
    # Sanity: each major tool family represented.
    assert any("light" in n for n in names)
    assert any("scene" in n for n in names)
    assert any("lock" in n for n in names)
    assert any("climate" in n for n in names)
