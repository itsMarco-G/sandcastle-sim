"""Force HA entity_ids for simulator entities to match the contract.

A defensive helper — needed only if HA's entity registry has stale
entries from earlier (broken) discovery payloads. A fresh
``make clean-ha && make up && make bootstrap && make run-sim`` will
not need this. Idempotent.

The mapping is the source of truth for which entity_id slug each
simulator unique_id should land on. If a future contract change
moves a slug, edit ``WANT`` here too.

Usage:
    .venv/bin/python scripts/normalize_entity_ids.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets

WS_URL = os.environ.get("HA_URL", "http://localhost:8123").replace("http", "ws") + "/api/websocket"
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# unique_id (sim_<domain>_<slug>) -> contract entity_id.
WANT: dict[str, str] = {
    "sim_light_living_room_main":         "light.living_room_main",
    "sim_light_living_room_accent":       "light.living_room_accent",
    "sim_light_kitchen_counter":          "light.kitchen_counter",
    "sim_light_hallway_ceiling":          "light.hallway_ceiling",
    "sim_light_bedroom_mood":             "light.bedroom_mood",
    "sim_light_bedroom_2_main":           "light.bedroom_2_main",
    "sim_switch_coffee_machine":          "switch.coffee_machine",
    "sim_lock_front_door":                "lock.front_door",
    "sim_cover_living_room_blind":        "cover.living_room_blind",
    "sim_cover_bedroom_blind":            "cover.bedroom_blind",
    "sim_climate_home_thermostat":        "climate.home_thermostat",
    "sim_sensor_bedroom_temperature":     "sensor.bedroom_temperature",
    "sim_sensor_bedroom_2_temperature":   "sensor.bedroom_2_temperature",
    "sim_sensor_power_meter":             "sensor.power_meter",
    "sim_binary_sensor_front_door_contact":     "binary_sensor.front_door_contact",
    "sim_binary_sensor_kitchen_window_contact": "binary_sensor.kitchen_window_contact",
    "sim_binary_sensor_hallway_motion":         "binary_sensor.hallway_motion",
    "sim_binary_sensor_living_room_motion":     "binary_sensor.living_room_motion",
    "sim_binary_sensor_kitchen_leak":           "binary_sensor.kitchen_leak",
    "sim_binary_sensor_hallway_smoke":          "binary_sensor.hallway_smoke",
    "sim_vacuum_robot_vacuum":            "vacuum.robot_vacuum",
}


async def main() -> int:
    if not HA_TOKEN:
        print("HA_TOKEN env var is empty — source .env first.", file=sys.stderr)
        return 2

    async with websockets.connect(WS_URL) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        ack = json.loads(await ws.recv())
        if ack.get("type") != "auth_ok":
            print(f"WS auth failed: {ack!r}", file=sys.stderr)
            return 1

        await ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
        entities = (json.loads(await ws.recv())).get("result", [])

        renamed = skipped = missing = 0
        nid = 2
        for uid, target in WANT.items():
            entry = next((e for e in entities if e.get("unique_id") == uid), None)
            if entry is None:
                print(f"  [-] {uid:50} (not in registry yet)")
                missing += 1
                continue
            if entry["entity_id"] == target:
                skipped += 1
                continue
            await ws.send(json.dumps({
                "id": nid,
                "type": "config/entity_registry/update",
                "entity_id": entry["entity_id"],
                "new_entity_id": target,
            }))
            r = json.loads(await ws.recv())
            ok = r.get("success", False)
            mark = "✓" if ok else "✗"
            print(f"  [{mark}] {entry['entity_id']:55} -> {target}")
            if ok:
                renamed += 1
            nid += 1

        print(
            f"\nRenamed {renamed}, already-correct {skipped}, "
            f"not-yet-registered {missing}"
        )
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
