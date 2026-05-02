"""Static device topology for the demo.

One source of truth for which devices exist, where they live, and
their initial state. Each entry maps cleanly onto the entity_id
convention from `docs/tool-contract.md`:

    {domain}.{slug}     e.g. light.living_room_main

Areas use the snake_case keys from the contract (`living_room`,
`kitchen`, ...). Their human-readable names — what HA's MQTT
discovery `suggested_area` field expects — are in `AREA_NAMES`.

Total: 22 devices across 6 areas.
"""

from __future__ import annotations

from typing import Optional, TypedDict


# Slug -> friendly name. The friendly names match the area registry
# created by scripts/bootstrap_ha.py so HA's MQTT-discovery
# suggested_area lookup resolves correctly.
AREA_NAMES = {
    "living_room": "Living Room",
    "kitchen": "Kitchen",
    "hallway": "Hallway",
    "bedroom": "Bedroom",
    "bedroom_2": "Bedroom 2",
    "bathroom": "Bathroom",
}


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


# Lights: 4 dimmable + 2 RGB, mixed across rooms (per brief).
#
# `name` is the device.name that HA slugifies to derive the
# entity_id. We pick names so slugify(name) matches `slug`. Friendly
# name in HA = the same string.
LIGHTS: list[DeviceSpec] = [
    {"slug": "living_room_main",   "area": "living_room", "name": "Living Room Main",   "kind": "dimmable"},
    {"slug": "living_room_accent", "area": "living_room", "name": "Living Room Accent", "kind": "rgb"},
    {"slug": "kitchen_counter",    "area": "kitchen",     "name": "Kitchen Counter",    "kind": "dimmable"},
    {"slug": "hallway_ceiling",    "area": "hallway",     "name": "Hallway Ceiling",    "kind": "dimmable"},
    {"slug": "bedroom_main",       "area": "bedroom",     "name": "Bedroom Main",       "kind": "dimmable"},
    {"slug": "bedroom_mood",       "area": "bedroom",     "name": "Bedroom Mood",       "kind": "rgb"},
    {"slug": "bedroom_2_main",     "area": "bedroom_2",   "name": "Bedroom 2 Main",     "kind": "dimmable"},
]

# Smart plug — coffee machine. 800 W is roughly a domestic espresso
# machine when actively pulling a shot. Brief says "with simulated
# wattage" — the power meter sums this when the plug is on.
SWITCHES: list[DeviceSpec] = [
    {"slug": "coffee_machine", "area": "kitchen", "name": "Coffee Machine", "watts_when_on": 800.0},
]

LOCKS: list[DeviceSpec] = [
    {"slug": "front_door", "area": "hallway", "name": "Front Door"},
]

# Blinds. 0=closed, 100=open per the contract.
COVERS: list[DeviceSpec] = [
    {"slug": "living_room_blind", "area": "living_room", "name": "Living Room Blind"},
    {"slug": "bedroom_blind",     "area": "bedroom",     "name": "Bedroom Blind"},
]

# Whole-home thermostat. Slug is `home_thermostat`, friendly name
# "Home Thermostat" — clearer for the GUI than just "Home".
# Contract entity_id: climate.home_thermostat.
CLIMATES: list[DeviceSpec] = [
    {"slug": "home_thermostat", "area": None, "name": "Home Thermostat"},
]

# Numeric sensors. Temperature in two bedrooms (drift in milestone 7),
# whole-home power meter (sum of active devices, also milestone 7).
SENSORS: list[DeviceSpec] = [
    {"slug": "bedroom_temperature",   "area": "bedroom",   "name": "Bedroom Temperature",   "device_class": "temperature", "unit": "°C", "initial": 21.0},
    {"slug": "bedroom_2_temperature", "area": "bedroom_2", "name": "Bedroom 2 Temperature", "device_class": "temperature", "unit": "°C", "initial": 20.5},
    {"slug": "power_meter",           "area": None,        "name": "Power Meter",           "device_class": "power",       "unit": "W",  "initial": 0.0},
    # Vacuum's current_room can't ride on the vacuum entity (HA's
    # mqtt.vacuum integration drops non-standard attributes), so we
    # surface it as a stand-alone sensor the GUI / tools can read.
    {"slug": "vacuum_current_room",   "area": None,        "name": "Vacuum Current Room",                                "unit": None, "initial": "docked"},
]

# Binary on/off sensors. device_class drives the icon HA picks.
BINARY_SENSORS: list[DeviceSpec] = [
    {"slug": "front_door_contact",     "area": "hallway",     "name": "Front Door Contact",     "device_class": "door"},
    {"slug": "kitchen_window_contact", "area": "kitchen",     "name": "Kitchen Window Contact", "device_class": "window"},
    {"slug": "hallway_motion",         "area": "hallway",     "name": "Hallway Motion",         "device_class": "motion"},
    {"slug": "living_room_motion",     "area": "living_room", "name": "Living Room Motion",     "device_class": "motion"},
    {"slug": "kitchen_leak",           "area": "kitchen",     "name": "Kitchen Leak",           "device_class": "moisture"},
    {"slug": "hallway_smoke",          "area": "hallway",     "name": "Hallway Smoke",          "device_class": "smoke"},
]

# Media player is deferred — HA's MQTT integration doesn't support
# the media_player domain, and modelling a speaker as switch+number
# would muddy the tool contract. See docs/tool-contract.md §4
# ("media_control" deferred to v0.2).
MEDIA_PLAYERS: list[DeviceSpec] = []

# Robot vacuum — dock/start/stop, with current_room attribute.
# Whole-home, so area is None.
VACUUMS: list[DeviceSpec] = [
    # Slug is `robot_vacuum` (matches slugify("Robot Vacuum")) so
    # entity_id = vacuum.robot_vacuum. Reserved name "robot" alone
    # would also work but reads less clearly in the GUI.
    {"slug": "robot_vacuum", "area": None, "name": "Robot Vacuum"},
]


# Iteration helper: every (domain, spec) pair across the topology.
ALL_BY_DOMAIN = {
    "light": LIGHTS,
    "switch": SWITCHES,
    "lock": LOCKS,
    "cover": COVERS,
    "climate": CLIMATES,
    "sensor": SENSORS,
    "binary_sensor": BINARY_SENSORS,
    "media_player": MEDIA_PLAYERS,
    "vacuum": VACUUMS,
}


def total_devices() -> int:
    return sum(len(specs) for specs in ALL_BY_DOMAIN.values())
