"""Milestone-7 acceptance: control surface + behaviours.

Drives every new control tool through the agent's registry (so we
exercise the same MCP path the agent's natural-language flow uses)
and observes the behaviour output via HA states.

Run with the simulator + smart-home MCP server already up.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx

# `mcp` is available wherever sandcastle-sim is installed (or via the
# venv's editable install).
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8765/mcp/")
HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")


def _unwrap(result):
    if getattr(result, "isError", False):
        blocks = getattr(result, "content", None) or []
        msg = getattr(blocks[0], "text", "MCP error") if blocks else "MCP error"
        return {"error": msg}
    blocks = getattr(result, "content", None) or []
    if blocks:
        txt = getattr(blocks[0], "text", None)
        if txt is not None:
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                return txt
    return getattr(result, "structuredContent", None)


def _ha_state(entity_id: str) -> dict:
    r = httpx.get(
        f"{HA_URL}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {HA_TOKEN}"},
        timeout=5,
    )
    return r.json()


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'✓' if ok else '✗'}] {label}{(' — ' + detail) if detail else ''}")
    return ok


async def main() -> int:
    failures = 0

    async with streamablehttp_client(MCP_URL) as (r, w, _gs):
        async with ClientSession(r, w) as session:
            await session.initialize()

            print("=== lock / unlock ===")
            res = _unwrap(await session.call_tool("unlock", {"entity_id": "lock.front_door"}))
            failures += not _check("unlock -> unlocked", res.get("state") == "unlocked",
                                   f"state={res.get('state')!r}")
            res = _unwrap(await session.call_tool("lock", {"entity_id": "lock.front_door"}))
            failures += not _check("lock -> locked", res.get("state") == "locked",
                                   f"state={res.get('state')!r}")

            print("\n=== set_cover_position ===")
            res = _unwrap(await session.call_tool(
                "set_cover_position", {"entity_id": "cover.living_room_blind", "position": 75}
            ))
            pos = (res.get("attributes") or {}).get("current_position")
            failures += not _check("blind -> 75%", pos == 75, f"position={pos!r}")
            res = _unwrap(await session.call_tool(
                "set_cover_position", {"entity_id": "cover.living_room_blind", "position": 0}
            ))
            pos = (res.get("attributes") or {}).get("current_position")
            failures += not _check("blind -> 0% (closed)", pos == 0, f"position={pos!r}")

            print("\n=== set_climate ===")
            res = _unwrap(await session.call_tool(
                "set_climate",
                {"entity_id": "climate.home_thermostat", "temperature": 22.5, "hvac_mode": "heat"},
            ))
            tgt = (res.get("attributes") or {}).get("temperature")
            failures += not _check("climate -> 22.5°C heat",
                                   res.get("state") == "heat" and tgt == 22.5,
                                   f"state={res.get('state')!r} tgt={tgt!r}")

            print("\n=== vacuum_control ===")
            res = _unwrap(await session.call_tool(
                "vacuum_control", {"entity_id": "vacuum.robot_vacuum", "action": "start"}
            ))
            failures += not _check("vacuum start -> cleaning",
                                   res.get("state") == "cleaning",
                                   f"state={res.get('state')!r}")
            res = _unwrap(await session.call_tool(
                "vacuum_control", {"entity_id": "vacuum.robot_vacuum", "action": "return_to_base"}
            ))
            failures += not _check("vacuum return -> returning",
                                   res.get("state") == "returning",
                                   f"state={res.get('state')!r}")

            print("\n=== validation paths ===")
            res = _unwrap(await session.call_tool(
                "set_cover_position",
                {"entity_id": "cover.living_room_blind", "position": "all the way"}
            ))
            failures += not _check("set_cover_position rejects non-int",
                                   isinstance(res, dict) and "error" in res)
            res = _unwrap(await session.call_tool(
                "set_climate", {"entity_id": "climate.home_thermostat", "hvac_mode": "frost"}
            ))
            failures += not _check("set_climate rejects bad mode",
                                   isinstance(res, dict) and "error" in res)
            res = _unwrap(await session.call_tool(
                "lock", {"entity_id": "light.kitchen_counter"}
            ))
            failures += not _check("lock rejects non-lock entity",
                                   isinstance(res, dict) and "error" in res)

    # ---- behaviours ----
    print("\n=== behaviours ===")
    print("(letting motion fire + temp drift over ~50s)")

    s_temp_before = float(_ha_state("sensor.bedroom_temperature")["state"])

    # Run coffee on, watch power kick up.
    httpx.post(
        f"{HA_URL}/api/services/switch/turn_on",
        headers={"Authorization": f"Bearer {HA_TOKEN}",
                 "content-type": "application/json"},
        json={"entity_id": "switch.coffee_machine"},
        timeout=5,
    )
    time.sleep(3.5)
    p_on = float(_ha_state("sensor.power_meter")["state"])
    failures += not _check("power_meter > 800W with coffee on",
                           p_on > 800,
                           f"P={p_on}W")
    httpx.post(
        f"{HA_URL}/api/services/switch/turn_off",
        headers={"Authorization": f"Bearer {HA_TOKEN}",
                 "content-type": "application/json"},
        json={"entity_id": "switch.coffee_machine"},
        timeout=5,
    )

    # Wait for at least one motion event + give temperature time to drift
    # (heat mode + 22.5°C target should pull the bedroom up slowly).
    deadline = time.time() + 50
    motion_seen = False
    while time.time() < deadline and not motion_seen:
        time.sleep(2)
        s = _ha_state("binary_sensor.hallway_motion")["state"]
        if s == "on":
            motion_seen = True
            break
    failures += not _check("hallway motion fired within 50s", motion_seen)

    s_temp_after = float(_ha_state("sensor.bedroom_temperature")["state"])
    failures += not _check("bedroom_temperature drifted",
                           s_temp_before != s_temp_after,
                           f"{s_temp_before} -> {s_temp_after}")

    print(f"\n=== {('PASS' if failures == 0 else 'FAIL')}: {failures} failure(s) ===")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
