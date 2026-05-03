"""Floor-plan layout: load, validate, auto-place devices.

The floor-plan layout (rooms + per-device x/y) lives as data in
JSON. The active path is the workdir copy
(``<workdir>/.sandcastle/floorplan.json``) when present, else the
bundled package seed (``data/seeds/floorplan.json``) — see
``resolve_floorplan_path``. This module is the canonical reader,
validator, and layout engine. Two consumers:

1. The control server (`simulator/control.py:floorplan`) reads + validates
   on every GET /api/floorplan. Bad JSON surfaces as a 500 with a clear
   error rather than letting the GUI crash on undefined fields.
2. The CLI (`sandcastle-sim floorplan auto`) reads the live HA entity
   list and writes a fresh floorplan.json with deterministic
   per-device-type placement.

Design notes:

- Rooms are axis-aligned rects today. The shape supports adding
  ``polygon`` later without a schema break — see ``_room_bounds``.
- Layout is deterministic given the same inventory + room rects. The
  agent never has to invent coordinates; if you want a creative
  re-layout, edit the JSON.
- Whole-home devices (``area: null``) skip layout entirely and render
  in the info bar regardless of (x, y).
"""

from __future__ import annotations

import json
import os
import shutil
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


class FloorplanError(ValueError):
    """Raised when floorplan.json is structurally invalid."""


# ---------------------------------------------------------------- paths
#
# Two storage locations:
#   - Package seed: read-only bundled demo (`data/seeds/floorplan.json`).
#     Updated by `git pull` / `pip install --upgrade`.
#   - Workdir copy: the user's home (`<workdir>/floorplan.json`).
#     Survives package updates because it lives outside the package.
#
# The active file is the workdir copy when present, the seed otherwise.


def _seed_floorplan_path() -> Path:
    """Path to the bundled-default floorplan.json inside the package."""
    p = resources.files("sandcastle_sim").joinpath(
        "data", "seeds", "floorplan.json"
    )
    return Path(str(p))


def _seed_topology_path() -> Path:
    """Path to the bundled-default topology.json inside the package."""
    p = resources.files("sandcastle_sim").joinpath(
        "data", "seeds", "topology.json"
    )
    return Path(str(p))


def _state_dir(workdir: Path) -> Path:
    """Hidden subdir for persistent sandcastle state (mirrors runtime.py)."""
    return Path(workdir) / ".sandcastle"


def resolve_floorplan_path(workdir: Optional[Path] = None) -> Path:
    """Return the active floorplan.json path.

    Order: ``<workdir>/.sandcastle/floorplan.json`` if seeded, else the
    bundled package seed. ``workdir`` falls back to the
    ``SANDCASTLE_WORKDIR`` env var if not passed explicitly.
    """
    if workdir is None:
        env = os.environ.get("SANDCASTLE_WORKDIR")
        workdir = Path(env) if env else None
    if workdir is not None:
        wd = _state_dir(workdir) / "floorplan.json"
        if wd.is_file():
            return wd
    return _seed_floorplan_path()


def resolve_images_dir(workdir: Optional[Path] = None) -> Optional[Path]:
    """Return ``<workdir>/.sandcastle/images/`` if it exists, else None."""
    if workdir is None:
        env = os.environ.get("SANDCASTLE_WORKDIR")
        workdir = Path(env) if env else None
    if workdir is None:
        return None
    d = _state_dir(workdir) / "images"
    return d if d.is_dir() else None


def seed_workdir(workdir: Path) -> Dict[str, Any]:
    """Copy bundled defaults into ``<workdir>/.sandcastle/`` if absent.

    Idempotent — never overwrites existing user files. Returns a dict
    of {created: [...], kept: [...]} useful for a startup log line.

    The state subdir mirrors `runtime.py`'s convention so all
    persistent sandcastle state (pids, logs, user's home) lives in
    one hidden directory.
    """
    state = _state_dir(workdir)
    state.mkdir(parents=True, exist_ok=True)
    (state / "images").mkdir(exist_ok=True)

    summary: Dict[str, Any] = {"created": [], "kept": []}
    for name, src in (
        ("floorplan.json", _seed_floorplan_path()),
        ("topology.json",  _seed_topology_path()),
    ):
        dst = state / name
        if dst.exists():
            summary["kept"].append(name)
        else:
            shutil.copy(src, dst)
            summary["created"].append(name)
    return summary


# Per-device-type placement priors. Each prior returns (x, y) in
# room-local coordinates given the room rect and an index/total for
# multi-device spreading. Crude but deterministic — good enough for a
# first-pass layout that the user/agent then nudges.
#
# Conventions in room-local space:
#   x = 0 is the room's left edge, y = 0 is the room's top edge.
#   "near a wall" = MARGIN pixels inset from the named edge.
MARGIN = 30


def _spread_x(room_w: int, idx: int, total: int) -> int:
    """Spread `total` items horizontally with even gaps; index 0 is leftmost."""
    if total <= 1:
        return room_w // 2
    # MARGIN inset on each side; divide remaining width into (total-1) gaps.
    span = max(0, room_w - 2 * MARGIN)
    step = span // max(1, total - 1)
    return MARGIN + idx * step


def _place_light(rw: int, rh: int, idx: int, total: int) -> Tuple[int, int]:
    # Lights along the upper third of the room, spread horizontally.
    return _spread_x(rw, idx, total), max(MARGIN, rh // 3)


def _place_temp(rw: int, rh: int, idx: int, total: int) -> Tuple[int, int]:
    # Temperature sensor: low-left interior wall by convention.
    return MARGIN + idx * 50, rh - MARGIN


def _place_motion(rw: int, rh: int, idx: int, total: int) -> Tuple[int, int]:
    # Motion: center-rear of room (sees the entrance).
    return _spread_x(rw, idx, total), max(MARGIN, rh // 2)


def _place_contact(rw: int, rh: int, idx: int, total: int) -> Tuple[int, int]:
    # Contact: along the bottom edge (typically a door/window).
    return _spread_x(rw, idx, total), rh - MARGIN


def _place_leak(rw: int, rh: int, idx: int, total: int) -> Tuple[int, int]:
    # Leak: low-right corner.
    return rw - MARGIN - idx * 50, rh - MARGIN


def _place_smoke(rw: int, rh: int, idx: int, total: int) -> Tuple[int, int]:
    # Smoke: ceiling-ish (top edge), spread horizontally.
    return _spread_x(rw, idx, total), MARGIN


def _place_lock(rw: int, rh: int, idx: int, total: int) -> Tuple[int, int]:
    # Lock: near a long wall, mid-height.
    return MARGIN + idx * 80, rh // 2


def _place_cover(rw: int, rh: int, idx: int, total: int) -> Tuple[int, int]:
    # Cover (blinds): along the top edge (windows are usually high).
    return _spread_x(rw, idx, total), MARGIN


def _place_switch(rw: int, rh: int, idx: int, total: int) -> Tuple[int, int]:
    # Generic switch: near a side wall, mid-height.
    return MARGIN + 80 + idx * 80, max(MARGIN, rh // 3)


def _place_default(rw: int, rh: int, idx: int, total: int) -> Tuple[int, int]:
    # Fallback: spread along the centre line.
    return _spread_x(rw, idx, total), rh // 2


_PLACERS = {
    "light":   _place_light,
    "temp":    _place_temp,
    "motion":  _place_motion,
    "contact": _place_contact,
    "leak":    _place_leak,
    "smoke":   _place_smoke,
    "lock":    _place_lock,
    "cover":   _place_cover,
    "switch":  _place_switch,
}

# Whole-home types — never get x/y, render in the info bar regardless.
WHOLE_HOME_TYPES = {"climate", "power", "vacuum"}

# All known device types. Anything outside this set is a layout warning,
# not an error — the GUI may still render it via the unknown-type fallback.
KNOWN_TYPES = set(_PLACERS) | WHOLE_HOME_TYPES


# ---------------------------------------------------------------- I/O


def load_floorplan(path: Path) -> Dict[str, Any]:
    """Read + validate floorplan.json. Returns the parsed dict on success."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    validate(data)
    return data


def save_floorplan(path: Path, data: Mapping[str, Any]) -> None:
    """Validate then write. Pretty-printed for human + diff readability."""
    validate(data)
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- validation


def validate(data: Any) -> None:
    """Raise FloorplanError if `data` doesn't match the expected shape."""
    if not isinstance(data, dict):
        raise FloorplanError("top-level must be an object")

    if "rooms" not in data or not isinstance(data["rooms"], dict):
        raise FloorplanError("`rooms` must be an object")
    if "devices" not in data or not isinstance(data["devices"], dict):
        raise FloorplanError("`devices` must be an object")

    # Optional `backdrop` — a path to an image file served by the
    # control server's /static/ handler. If set, the GUI renders the
    # image as the floor and skips the JS-drawn walls/tints/furniture.
    if "backdrop" in data and not isinstance(data["backdrop"], str):
        raise FloorplanError("`backdrop` must be a string (image filename) if present")

    for key, room in data["rooms"].items():
        if not isinstance(room, dict):
            raise FloorplanError(f"room {key!r} must be an object")
        for f in ("x", "y", "w", "h"):
            if not isinstance(room.get(f), (int, float)):
                raise FloorplanError(f"room {key!r} missing numeric {f}")
        if room["w"] <= 0 or room["h"] <= 0:
            raise FloorplanError(f"room {key!r} must have positive w/h")

    rooms = data["rooms"]
    for entity_id, dev in data["devices"].items():
        if not isinstance(dev, dict):
            raise FloorplanError(f"device {entity_id!r} must be an object")
        if "type" not in dev:
            raise FloorplanError(f"device {entity_id!r} missing `type`")
        area = dev.get("area")
        if area is None:
            # Whole-home device — no further checks.
            continue
        if area not in rooms:
            raise FloorplanError(
                f"device {entity_id!r} references unknown area {area!r}"
            )
        for f in ("x", "y"):
            if not isinstance(dev.get(f), (int, float)):
                raise FloorplanError(
                    f"device {entity_id!r} missing numeric {f}"
                )
        # Bounds check: x/y are room-local; must be inside the room rect.
        room = rooms[area]
        if not (0 <= dev["x"] <= room["w"] and 0 <= dev["y"] <= room["h"]):
            raise FloorplanError(
                f"device {entity_id!r} at ({dev['x']}, {dev['y']}) "
                f"falls outside area {area!r} (w={room['w']}, h={room['h']})"
            )


# ---------------------------------------------------------------- layout


def auto_layout(
    inventory: List[Dict[str, Any]],
    rooms: Dict[str, Dict[str, Any]],
    *,
    existing: Optional[Dict[str, Dict[str, Any]]] = None,
    force: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Place each entity at deterministic coordinates inside its room.

    Args:
        inventory: list of device dicts with keys ``entity_id``, ``area``,
            ``type``. ``area`` may be ``None`` for whole-home devices.
        rooms: room-name -> {x, y, w, h, ...}.
        existing: previous floorplan ``devices`` map. When ``force`` is
            False, devices already present here keep their current
            positions; only new entities are placed.
        force: if True, every device is re-placed from scratch.

    Returns:
        A new ``devices`` map suitable for the floorplan.json schema.
    """
    out: Dict[str, Dict[str, Any]] = {}
    existing = existing or {}

    # Group by (area, type) to derive (idx, total) for spreading.
    grouped: Dict[Tuple[Optional[str], str], List[Dict[str, Any]]] = {}
    for entry in inventory:
        key = (entry.get("area"), entry.get("type") or "")
        grouped.setdefault(key, []).append(entry)

    for (area, dtype), entries in grouped.items():
        # Stable order so re-runs produce identical output.
        entries.sort(key=lambda e: e["entity_id"])

        # Whole-home types: emit without x/y.
        if area is None or dtype in WHOLE_HOME_TYPES:
            for e in entries:
                out[e["entity_id"]] = {"area": area, "type": dtype}
            continue

        if area not in rooms:
            # Skip silently — caller may have mismatched HA areas with
            # floor plan. The validator would catch it, but here we
            # just don't place rather than failing the whole layout.
            continue

        room = rooms[area]
        rw, rh = int(room["w"]), int(room["h"])
        placer = _PLACERS.get(dtype, _place_default)

        for idx, e in enumerate(entries):
            eid = e["entity_id"]
            keep_existing = (
                not force
                and eid in existing
                and existing[eid].get("area") == area
                and "x" in existing[eid]
                and "y" in existing[eid]
            )
            if keep_existing:
                out[eid] = dict(existing[eid])
                continue
            x, y = placer(rw, rh, idx, len(entries))
            entry: Dict[str, Any] = {"area": area, "type": dtype, "x": x, "y": y}
            # Preserve secondary attributes (e.g. rgb: true on lights)
            # from existing or inventory if present.
            for key in ("rgb",):
                if eid in existing and key in existing[eid]:
                    entry[key] = existing[eid][key]
                elif key in e:
                    entry[key] = e[key]
            out[eid] = entry

    return out


# ---------------------------------------------------------------- helpers


def _room_bounds(room: Mapping[str, Any]) -> Tuple[int, int]:
    """Return (w, h). Future polygon support would compute a bounding box here."""
    return int(room["w"]), int(room["h"])
