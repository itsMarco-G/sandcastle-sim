"""Unit tests for the floor-plan validator and layout engine.

Pure-logic tests — no HA, no MCP, no asyncio. The CLI integration
that calls MCP is exercised by the end-to-end flow rather than here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandcastle_sim import floorplan as fp


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def rooms() -> dict:
    return {
        "kitchen":     {"x": 0,   "y": 330, "w": 350,  "h": 270, "name": "Kitchen"},
        "living_room": {"x": 350, "y": 330, "w": 750,  "h": 270, "name": "Living Room"},
        "bedroom":     {"x": 0,   "y": 0,   "w": 380,  "h": 260, "name": "Bedroom"},
    }


@pytest.fixture
def minimal(rooms) -> dict:
    return {
        "version": 1,
        "rooms": rooms,
        "devices": {
            "light.kitchen_main": {"area": "kitchen", "type": "light", "x": 100, "y": 80},
        },
    }


# ---------------------------------------------------------------- validation


def test_validate_accepts_minimal(minimal):
    fp.validate(minimal)  # no raise


def test_validate_rejects_unknown_area(minimal):
    minimal["devices"]["light.foo"] = {"area": "no_such_room", "type": "light", "x": 1, "y": 1}
    with pytest.raises(fp.FloorplanError, match="unknown area"):
        fp.validate(minimal)


def test_validate_rejects_out_of_bounds(minimal, rooms):
    # Kitchen is 350×270; place at x=400 to violate the right edge.
    minimal["devices"]["light.oob"] = {"area": "kitchen", "type": "light", "x": 400, "y": 50}
    with pytest.raises(fp.FloorplanError, match="outside area"):
        fp.validate(minimal)


def test_validate_rejects_missing_xy_for_room_device(minimal):
    minimal["devices"]["light.no_xy"] = {"area": "kitchen", "type": "light"}
    with pytest.raises(fp.FloorplanError, match="missing numeric"):
        fp.validate(minimal)


def test_validate_allows_whole_home_no_xy(minimal):
    minimal["devices"]["climate.home_thermostat"] = {"area": None, "type": "climate"}
    fp.validate(minimal)  # no raise


def test_validate_rejects_non_dict_top_level():
    with pytest.raises(fp.FloorplanError, match="top-level"):
        fp.validate([1, 2, 3])


def test_validate_rejects_zero_size_room(minimal):
    minimal["rooms"]["bad"] = {"x": 0, "y": 0, "w": 0, "h": 100, "name": "x"}
    with pytest.raises(fp.FloorplanError, match="positive"):
        fp.validate(minimal)


def test_validate_accepts_backdrop(minimal):
    minimal["backdrop"] = "my_home.png"
    fp.validate(minimal)  # no raise


def test_validate_rejects_non_string_backdrop(minimal):
    minimal["backdrop"] = 42
    with pytest.raises(fp.FloorplanError, match="backdrop"):
        fp.validate(minimal)


# ---------------------------------------------------------------- layout


def test_auto_layout_places_inside_room(rooms):
    inv = [
        {"entity_id": "light.kitchen_main", "area": "kitchen", "type": "light"},
        {"entity_id": "binary_sensor.kitchen_window_contact", "area": "kitchen", "type": "contact"},
    ]
    out = fp.auto_layout(inv, rooms, force=True)

    for eid, dev in out.items():
        room = rooms[dev["area"]]
        assert 0 <= dev["x"] <= room["w"], f"{eid} x out of bounds"
        assert 0 <= dev["y"] <= room["h"], f"{eid} y out of bounds"


def test_auto_layout_is_deterministic(rooms):
    inv = [
        {"entity_id": "light.kitchen_main", "area": "kitchen", "type": "light"},
        {"entity_id": "light.kitchen_island", "area": "kitchen", "type": "light"},
        {"entity_id": "switch.coffee_machine", "area": "kitchen", "type": "switch"},
    ]
    a = fp.auto_layout(inv, rooms, force=True)
    b = fp.auto_layout(list(reversed(inv)), rooms, force=True)
    # Same inventory, regardless of input order, produces same coords.
    assert a == b


def test_auto_layout_preserves_existing_when_not_forced(rooms):
    inv = [
        {"entity_id": "light.kitchen_main", "area": "kitchen", "type": "light"},
    ]
    existing = {
        "light.kitchen_main": {"area": "kitchen", "type": "light", "x": 42, "y": 42},
    }
    out = fp.auto_layout(inv, rooms, existing=existing, force=False)
    assert out["light.kitchen_main"]["x"] == 42
    assert out["light.kitchen_main"]["y"] == 42


def test_auto_layout_force_overrides_existing(rooms):
    inv = [
        {"entity_id": "light.kitchen_main", "area": "kitchen", "type": "light"},
    ]
    existing = {
        "light.kitchen_main": {"area": "kitchen", "type": "light", "x": 42, "y": 42},
    }
    out = fp.auto_layout(inv, rooms, existing=existing, force=True)
    # The deterministic layout for one light in the kitchen does not
    # land at (42, 42) — verify the override took effect.
    assert (out["light.kitchen_main"]["x"], out["light.kitchen_main"]["y"]) != (42, 42)


def test_auto_layout_keeps_rgb_attribute(rooms):
    inv = [
        {"entity_id": "light.bedroom_mood", "area": "bedroom", "type": "light", "rgb": True},
    ]
    out = fp.auto_layout(inv, rooms, force=True)
    assert out["light.bedroom_mood"].get("rgb") is True


def test_auto_layout_skips_unknown_areas(rooms):
    inv = [
        {"entity_id": "light.attic_main", "area": "attic", "type": "light"},
    ]
    out = fp.auto_layout(inv, rooms, force=True)
    # Attic isn't a known room — device is silently skipped.
    assert "light.attic_main" not in out


def test_auto_layout_handles_whole_home(rooms):
    inv = [
        {"entity_id": "climate.home_thermostat", "area": None, "type": "climate"},
    ]
    out = fp.auto_layout(inv, rooms, force=True)
    assert out["climate.home_thermostat"] == {"area": None, "type": "climate"}


def test_auto_layout_spreads_multiple_lights(rooms):
    inv = [
        {"entity_id": "light.living_a", "area": "living_room", "type": "light"},
        {"entity_id": "light.living_b", "area": "living_room", "type": "light"},
        {"entity_id": "light.living_c", "area": "living_room", "type": "light"},
    ]
    out = fp.auto_layout(inv, rooms, force=True)
    xs = sorted(out[k]["x"] for k in out)
    # Three lights should land at three distinct x's.
    assert len(set(xs)) == 3


# ---------------------------------------------------------------- I/O


def test_load_round_trip_default_floorplan(tmp_path):
    """The bundled seed floorplan.json loads + re-saves identically."""
    src = fp._seed_floorplan_path()
    data = fp.load_floorplan(src)
    out = tmp_path / "round_trip.json"
    fp.save_floorplan(out, data)
    again = fp.load_floorplan(out)
    assert data == again


def test_save_rejects_invalid(tmp_path):
    bad = {"rooms": {}, "devices": {"x.y": {}}}  # missing type
    with pytest.raises(fp.FloorplanError):
        fp.save_floorplan(tmp_path / "bad.json", bad)


# ---------------------------------------------------------------- workdir override


def test_resolve_floorplan_path_falls_back_to_seed_when_workdir_empty(tmp_path):
    """No workdir copy → resolver returns the package seed."""
    p = fp.resolve_floorplan_path(tmp_path)
    assert p == fp._seed_floorplan_path()


def test_resolve_floorplan_path_prefers_workdir_when_seeded(tmp_path):
    """Workdir copy exists → resolver returns the workdir path."""
    state = tmp_path / ".sandcastle"
    state.mkdir()
    wd_file = state / "floorplan.json"
    wd_file.write_text(fp._seed_floorplan_path().read_text())
    p = fp.resolve_floorplan_path(tmp_path)
    assert p == wd_file


def test_resolve_images_dir_returns_none_when_absent(tmp_path):
    assert fp.resolve_images_dir(tmp_path) is None


def test_resolve_images_dir_returns_dir_when_present(tmp_path):
    images = tmp_path / ".sandcastle" / "images"
    images.mkdir(parents=True)
    assert fp.resolve_images_dir(tmp_path) == images


def test_seed_workdir_creates_files_first_time(tmp_path):
    summary = fp.seed_workdir(tmp_path)
    assert sorted(summary["created"]) == ["floorplan.json", "topology.json"]
    assert summary["kept"] == []
    assert (tmp_path / ".sandcastle" / "floorplan.json").is_file()
    assert (tmp_path / ".sandcastle" / "topology.json").is_file()
    assert (tmp_path / ".sandcastle" / "images").is_dir()


def test_seed_workdir_is_idempotent(tmp_path):
    """Re-seeding never overwrites existing user files."""
    fp.seed_workdir(tmp_path)
    # User edits their floor plan
    user_path = tmp_path / ".sandcastle" / "floorplan.json"
    user_path.write_text('{"rooms":{},"devices":{},"customised":true}')
    summary = fp.seed_workdir(tmp_path)
    assert summary["created"] == []
    assert sorted(summary["kept"]) == ["floorplan.json", "topology.json"]
    # User's edit survived.
    assert "customised" in user_path.read_text()


def test_seed_workdir_seeds_only_what_is_missing(tmp_path):
    """Half-seeded workdir: only the missing file gets created."""
    state = tmp_path / ".sandcastle"
    state.mkdir(parents=True)
    # Pre-populate only floorplan.
    (state / "floorplan.json").write_text('{"rooms":{},"devices":{}}')
    summary = fp.seed_workdir(tmp_path)
    assert summary["kept"] == ["floorplan.json"]
    assert summary["created"] == ["topology.json"]


def test_resolve_via_env_var(tmp_path, monkeypatch):
    """Workdir resolves from $SANDCASTLE_WORKDIR when not passed."""
    monkeypatch.setenv("SANDCASTLE_WORKDIR", str(tmp_path))
    fp.seed_workdir(tmp_path)
    p = fp.resolve_floorplan_path()  # no arg
    assert p == tmp_path / ".sandcastle" / "floorplan.json"
