"""Smoke-test the smart-home MCP server end-to-end.

Connects via streamable HTTP, calls each discovery tool, prints a
short report. Used to validate milestone 3 and as a quick-sanity
check after later changes.

Usage (after `make up && make bootstrap && make run-mcp` is up):
    .venv/bin/python scripts/smoketest_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8765/mcp/")


def _unwrap(result) -> object:
    """Pull the JSON payload out of an MCP CallToolResult.

    FastMCP serialises the tool return into both ``content[0].text``
    (unwrapped JSON) and ``structuredContent`` (wrapped under a
    ``result`` key for spec compat). We prefer the text path so the
    returned shape matches the contract doc exactly.
    """
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


async def main() -> int:
    print(f"Connecting to {MCP_URL}")
    async with streamablehttp_client(MCP_URL) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"\nTools advertised by server: {len(tools.tools)}")
            for t in tools.tools:
                params = list(t.inputSchema.get("properties", {}).keys())
                print(f"  - {t.name}({', '.join(params)})")

            print("\n--- list_areas ---")
            r = await session.call_tool("list_areas", {})
            data = _unwrap(r)
            print(json.dumps(data, indent=2))

            print("\n--- list_devices() ---")
            r = await session.call_tool("list_devices", {})
            data = _unwrap(r)
            assert isinstance(data, dict), f"expected dict, got {type(data)}"
            print(f"count: {data['count']}")
            for d in data["devices"]:
                print(
                    f"  {d['entity_id']:35} area={d['area']!s:14} "
                    f"protocol={d['protocol']:7} state={d['state']}"
                )

            print("\n--- list_devices(domain='light') ---")
            r = await session.call_tool("list_devices", {"domain": "light"})
            data = _unwrap(r)
            print(f"count: {data['count']}")
            for d in data["devices"]:
                print(f"  {d['entity_id']}  friendly_name={d['friendly_name']!r}")

            if data["count"] > 0:
                eid = data["devices"][0]["entity_id"]
                print(f"\n--- get_device_state({eid!r}) ---")
                r = await session.call_tool("get_device_state", {"entity_id": eid})
                print(json.dumps(_unwrap(r), indent=2))

                print("\n--- get_device_state('light.does_not_exist') ---")
                r = await session.call_tool(
                    "get_device_state", {"entity_id": "light.does_not_exist"}
                )
                print(json.dumps(_unwrap(r), indent=2))

    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
