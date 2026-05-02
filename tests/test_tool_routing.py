"""Unit tests for the agent's keyword-based tool router.

The router is a hot path on the latency-critical iter-1 prompt
eval. Regressions here directly cost users seconds per turn, so
this is the one bit of agent logic that earns explicit tests
(versus full integration coverage in the integration workflow).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from sandcastle_sim.agent.one_shot import _route_tools


# Minimal fake MCP tool list — names cover every family the
# router knows about plus a few generic discovery tools.
_ALL_NAMES = [
    "list_areas", "list_devices", "get_device_state",
    "turn_on", "turn_off", "set_light",
    "lock", "unlock",
    "set_cover_position",
    "set_climate",
    "vacuum_control",
    "apply_scene", "apply_states",
    "save_scene", "delete_scene",
    "list_recent_events",
]


def _make_tools(names: List[str]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": n, "description": "", "parameters": {}},
        }
        for n in names
    ]


def _names(routed: List[Dict[str, Any]]) -> set:
    return {t["function"]["name"] for t in routed}


@pytest.fixture
def all_tools() -> List[Dict[str, Any]]:
    return _make_tools(_ALL_NAMES)


def test_light_intent_routes_to_light_tools(all_tools):
    routed = _names(_route_tools(all_tools, "turn off the kitchen light"))
    assert {"turn_on", "turn_off", "set_light"} <= routed
    assert "lock" not in routed
    assert "set_climate" not in routed


def test_lock_intent_routes_to_lock_tools(all_tools):
    routed = _names(_route_tools(all_tools, "lock the front door"))
    assert {"lock", "unlock"} <= routed
    assert "vacuum_control" not in routed


def test_scene_intent_routes_to_scene_tools(all_tools):
    routed = _names(_route_tools(all_tools, "set up movie night"))
    assert "apply_scene" in routed


def test_climate_intent_routes_to_climate_tool(all_tools):
    routed = _names(_route_tools(all_tools, "make it warmer in here"))
    assert "set_climate" in routed


def test_cover_intent_routes_to_cover_tool(all_tools):
    routed = _names(_route_tools(all_tools, "open the bedroom blinds"))
    assert "set_cover_position" in routed


def test_get_device_state_always_included_when_any_match(all_tools):
    """get_device_state is the post-action verify tool — should
    always tag along with control intents so the model can confirm
    what it just changed."""
    routed = _names(_route_tools(all_tools, "turn off the kitchen light"))
    assert "get_device_state" in routed


def test_unknown_intent_falls_back_to_full_set(all_tools):
    """No keyword match -> send everything so the model still has
    a chance at out-of-vocabulary requests."""
    routed = _route_tools(all_tools, "tell me a poem about pelicans")
    assert _names(routed) == set(_ALL_NAMES)


def test_empty_tool_list_returns_empty(all_tools):
    routed = _route_tools([], "turn off the kitchen light")
    assert routed == []


def test_multi_intent_unions_families(all_tools):
    """A prompt that spans two families should pick up tools from both."""
    routed = _names(_route_tools(
        all_tools, "lock the front door and turn off the kitchen light",
    ))
    assert {"turn_on", "turn_off", "set_light"} <= routed
    assert {"lock", "unlock"} <= routed
