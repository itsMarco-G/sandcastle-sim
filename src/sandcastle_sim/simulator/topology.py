"""Static device topology for the simulator.

Source of truth is JSON. The active file is, in order:

1. ``$SANDCASTLE_WORKDIR/topology.json`` — the user's home, edited by
   them or by their coding agent. Survives ``git pull`` and
   ``pip install --upgrade`` because it lives outside the package.
2. ``sandcastle_sim/data/seeds/topology.json`` — the bundled demo (the
   six-room apartment with 22 simulated devices). Read-only from the
   user's perspective; updated by package upgrades.

Module exposes the same constants the rest of the simulator imports
(``AREA_NAMES``, ``LIGHTS``, ``SWITCHES``, …) so callers don't need
to change. Loading happens at import time; the simulator subprocess
is started by the CLI after ``SANDCASTLE_WORKDIR`` is set, so the
workdir override is in effect by the time anything imports this.

The JSON shape:

    {
      "area_names": { "kitchen": "Kitchen", ... },
      "devices": {
        "light":         [ {slug, area, name, kind, ...}, ... ],
        "switch":        [ ... ],
        "lock":          [ ... ],
        "cover":         [ ... ],
        "climate":       [ ... ],
        "sensor":        [ ... ],
        "binary_sensor": [ ... ],
        "media_player":  [ ... ],
        "vacuum":        [ ... ]
      }
    }

Each spec maps cleanly onto entity_id `{domain}.{slug}` per the
contract in `docs/tool-contract.md`. Domain-specific extras
(``kind``, ``device_class``, ``watts_when_on``, …) live in the spec
dict and are picked up by the corresponding Device subclass.
"""

from __future__ import annotations

import json
import logging
import os
from importlib import resources
from pathlib import Path
from typing import Optional, TypedDict


log = logging.getLogger(__name__)


class DeviceSpec(TypedDict, total=False):
    slug: str
    area: Optional[str]   # snake_case area key, or None for whole-home
    name: str             # friendly name
    # domain-specific extras live in this dict, picked up by the
    # device subclass (kind, device_class, watts_when_on, etc.)
    kind: str
    device_class: str
    unit: str
    initial: float
    watts_when_on: float


def _load() -> dict:
    """Resolve workdir-or-package and parse the topology JSON.

    Looks for ``$SANDCASTLE_WORKDIR/.sandcastle/topology.json`` first
    so the demo's bundled seed is never written back to. Falls back
    to the package seed if the workdir copy is missing.
    """
    workdir = os.environ.get("SANDCASTLE_WORKDIR")
    if workdir:
        wd_path = Path(workdir) / ".sandcastle" / "topology.json"
        if wd_path.is_file():
            with open(wd_path, "r", encoding="utf-8") as f:
                return json.load(f)

    seed = resources.files("sandcastle_sim").joinpath(
        "data", "seeds", "topology.json"
    )
    return json.loads(seed.read_text(encoding="utf-8"))


_DATA = _load()

# Slug -> friendly name. The friendly names match the area registry
# created by the bootstrap script so HA's MQTT-discovery
# suggested_area lookup resolves correctly.
AREA_NAMES: dict[str, str] = dict(_DATA.get("area_names") or {})

_DEVICES = _DATA.get("devices") or {}

LIGHTS:         list[DeviceSpec] = list(_DEVICES.get("light",         []))
SWITCHES:       list[DeviceSpec] = list(_DEVICES.get("switch",        []))
LOCKS:          list[DeviceSpec] = list(_DEVICES.get("lock",          []))
COVERS:         list[DeviceSpec] = list(_DEVICES.get("cover",         []))
CLIMATES:       list[DeviceSpec] = list(_DEVICES.get("climate",       []))
SENSORS:        list[DeviceSpec] = list(_DEVICES.get("sensor",        []))
BINARY_SENSORS: list[DeviceSpec] = list(_DEVICES.get("binary_sensor", []))
MEDIA_PLAYERS:  list[DeviceSpec] = list(_DEVICES.get("media_player",  []))
VACUUMS:        list[DeviceSpec] = list(_DEVICES.get("vacuum",        []))


# Iteration helper: every (domain, spec) pair across the topology.
ALL_BY_DOMAIN = {
    "light":         LIGHTS,
    "switch":        SWITCHES,
    "lock":          LOCKS,
    "cover":         COVERS,
    "climate":       CLIMATES,
    "sensor":        SENSORS,
    "binary_sensor": BINARY_SENSORS,
    "media_player":  MEDIA_PLAYERS,
    "vacuum":        VACUUMS,
}


def total_devices() -> int:
    return sum(len(specs) for specs in ALL_BY_DOMAIN.values())
