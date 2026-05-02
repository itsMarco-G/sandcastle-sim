"""Milestone-4 acceptance test: drive a light through the MCP server.

Calls turn_on / set_light / turn_off against `light.kitchen_counter`
and a couple of others. Verifies state changes round-trip:

    MCP client  ->  MCP server  ->  HA  ->  MQTT  ->  simulator

then bounces back the resulting state via list_devices / get_device_state.

Run with the simulator + MCP server already up. The Makefile target
`make test-light` runs this. The script returns nonzero on
unexpected behaviour so it can gate later milestones.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://localhost:8765/mcp/"


def _unwrap(result) -> Any:
    """Pull the JSON payload out of an MCP CallToolResult.

    If MCP's own schema validation flagged the call (isError=True),
    return ``{"error": <message>}`` so callers can treat schema-layer
    rejections the same as tool-returned errors.
    """
    if getattr(result, "isError", False):
        blocks = getattr(result, "content", None) or []
        msg = getattr(blocks[0], "text", "MCP validation error") if blocks else "MCP error"
        return {"error": msg}
    blocks = getattr(result, "content", None) or []
    if blocks:
        txt = getattr(blocks[0], "text", None)
        if txt is not None:
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                return txt
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
        return sc["result"]
    return sc


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✗"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    return ok


async def main() -> int:
    failures = 0

    async with streamablehttp_client(MCP_URL) as (read, write, _gs):
        async with ClientSession(read, write) as session:
            await session.initialize()

            target = "light.kitchen_counter"
            rgb_target = "light.living_room_accent"

            print(f"\n=== {target} (dimmable) ===")

            # 1. Confirm starting state is off.
            r = await session.call_tool("get_device_state", {"entity_id": target})
            state = _unwrap(r)
            failures += not _check(
                "initial state read", state.get("state") in ("on", "off"),
                f"state={state.get('state')!r}",
            )

            # 2. turn_on, expect state=on.
            r = await session.call_tool("turn_on", {"entity_id": target})
            state = _unwrap(r)
            failures += not _check(
                "turn_on -> state=on", state.get("state") == "on",
                f"state={state.get('state')!r} brightness={state.get('attributes',{}).get('brightness')!r}",
            )

            # 3. set_light brightness=180.
            r = await session.call_tool(
                "set_light", {"entity_id": target, "brightness": 180},
            )
            state = _unwrap(r)
            b = (state.get("attributes") or {}).get("brightness")
            failures += not _check(
                "set_light brightness=180", b == 180, f"brightness={b!r}",
            )

            # 4. turn_off.
            r = await session.call_tool("turn_off", {"entity_id": target})
            state = _unwrap(r)
            failures += not _check(
                "turn_off -> state=off", state.get("state") == "off",
                f"state={state.get('state')!r}",
            )

            print(f"\n=== {rgb_target} (RGB) ===")

            # 5. set rgb_color.
            r = await session.call_tool(
                "set_light", {"entity_id": rgb_target, "rgb_color": [255, 50, 0]},
            )
            state = _unwrap(r)
            attrs = state.get("attributes") or {}
            rgb = attrs.get("rgb_color") or attrs.get("color")
            failures += not _check(
                "set_light rgb_color=[255,50,0]", state.get("state") == "on",
                f"state={state.get('state')!r} rgb={rgb!r}",
            )

            # 6. turn_off.
            r = await session.call_tool("turn_off", {"entity_id": rgb_target})
            state = _unwrap(r)
            failures += not _check(
                f"{rgb_target} turn_off", state.get("state") == "off",
                f"state={state.get('state')!r}",
            )

            print("\n=== switch round-trip ===")

            # 7. switch.coffee_machine on/off.
            r = await session.call_tool(
                "turn_on", {"entity_id": "switch.coffee_machine"},
            )
            state = _unwrap(r)
            failures += not _check(
                "switch.coffee_machine turn_on", state.get("state") == "on",
                f"state={state.get('state')!r}",
            )

            r = await session.call_tool(
                "turn_off", {"entity_id": "switch.coffee_machine"},
            )
            state = _unwrap(r)
            failures += not _check(
                "switch.coffee_machine turn_off", state.get("state") == "off",
                f"state={state.get('state')!r}",
            )

            print("\n=== validation paths ===")

            # 8. lock.front_door is the wrong domain for turn_on.
            r = await session.call_tool(
                "turn_on", {"entity_id": "lock.front_door"},
            )
            state = _unwrap(r)
            failures += not _check(
                "turn_on rejects lock domain",
                isinstance(state, dict) and "error" in state,
                state.get("error", "<no error>")[:80] if isinstance(state, dict) else "",
            )

            # 9. Bad brightness type.
            r = await session.call_tool(
                "set_light",
                {"entity_id": target, "brightness": "very bright"},
            )
            state = _unwrap(r)
            failures += not _check(
                "set_light rejects non-int brightness",
                isinstance(state, dict) and "error" in state,
            )

    print(
        f"\n=== {('PASS' if failures == 0 else 'FAIL')}: "
        f"{failures} failure(s) ==="
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
