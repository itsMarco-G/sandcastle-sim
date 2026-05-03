"""Control HTTP server for the simulator.

Two purposes:

1. Serve the floor-plan GUI (single static HTML file).
2. Expose a demo-trigger API for entities that don't have an HA
   service (motion / contact / leak / smoke sensors). The architecture
   keeps the GUI strictly off MQTT, so it asks the simulator to fire
   a sensor event and the simulator does the MQTT publish itself.

Endpoints:

    GET  /                   -> gui/index.html
    GET  /static/<file>      -> gui/<file> (CSS/JS if we ever split out)
    GET  /api/config         -> {"ha_url": ..., "ha_token": ...}
    GET  /api/floorplan      -> floor-plan rooms + device positions JSON
    POST /api/demo/trigger   -> {entity_id, action} -> publishes MQTT
    GET  /api/health         -> {"ok": true}

The control server runs in the same asyncio event loop as the MQTT
dispatcher; both share the same aiomqtt.Client so trigger handlers
publish via the existing connection.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
from aiohttp import web

from .base import Device
from .sensors import BinarySensor

log = logging.getLogger(__name__)

# Path to the GUI folder. Lives inside the package as data so it
# travels with `pip install sandcastle-sim`.
# .../sandcastle_sim/simulator/control.py -> .../sandcastle_sim/data/gui/
GUI_DIR = Path(__file__).resolve().parent.parent / "data" / "gui"

DEFAULT_HOST = os.environ.get("CONTROL_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("CONTROL_PORT", "8766"))


def _build_app(devices: List[Device]) -> web.Application:
    """Build the aiohttp application.

    `devices` is the list the simulator owns; we index by entity-id
    style key (`{domain}.{slug}`) so trigger requests can find the
    right device. Note: that key matches the contract entity_id only
    when the simulator's slug == HA's entity_id slug, which holds
    after milestone 4's topology naming.
    """
    by_entity_id: Dict[str, Device] = {
        f"{d.domain}.{d.slug}": d for d in devices
    }

    async def index(_request: web.Request) -> web.Response:
        path = GUI_DIR / "index.html"
        if not path.is_file():
            return web.Response(
                status=404,
                text=f"GUI not bundled at {path}.",
            )
        return web.FileResponse(path)

    async def config(_request: web.Request) -> web.Response:
        # The GUI needs a long-lived HA token + URL to open its own
        # WebSocket subscription. We surface them via this endpoint
        # (rather than baking them into the HTML) so the GUI can be
        # re-served without rebuilding when the token rotates.
        ha_url = os.environ.get("HA_URL", "http://localhost:8123")
        ha_token = os.environ.get("HA_TOKEN", "")
        if not ha_token:
            return web.json_response(
                {"error": "HA_TOKEN env var is empty — run `make bootstrap`."},
                status=500,
            )
        return web.json_response({"ha_url": ha_url, "ha_token": ha_token})

    async def demo_trigger(request: web.Request) -> web.Response:
        """Fire a binary-sensor event from the GUI.

        Body:
            {
              "entity_id": "binary_sensor.hallway_motion",
              "action": "pulse" | "set",
              "state": "ON" | "OFF",   // for action=="set"
              "duration_ms": 3000      // for action=="pulse"
            }

        For motion / leak / smoke we use `pulse` (auto-clear). For
        door/window contacts we use `set` so the user can leave the
        door open.

        Locks aren't routed through here — they go through HA's
        lock service which the agent's `lock` / `unlock` tools also
        use, keeping the demo's narrative consistent.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "body must be JSON"}, status=400,
            )

        entity_id = body.get("entity_id")
        action = body.get("action", "pulse")
        if not isinstance(entity_id, str) or not entity_id:
            return web.json_response(
                {"error": "entity_id required"}, status=400,
            )
        device = by_entity_id.get(entity_id)
        if device is None:
            return web.json_response(
                {"error": f"unknown entity_id: {entity_id}"}, status=404,
            )
        if not isinstance(device, BinarySensor):
            return web.json_response(
                {"error": "demo trigger only works for binary_sensor entities"},
                status=400,
            )

        if action == "set":
            state = str(body.get("state", "")).upper()
            if state not in ("ON", "OFF"):
                return web.json_response(
                    {"error": "state must be ON or OFF"}, status=400,
                )
            await device.set(state == "ON")
            log.info("demo set %s = %s", entity_id, state)
            return web.json_response({"entity_id": entity_id, "state": state})

        if action == "pulse":
            duration_ms = int(body.get("duration_ms", 3000))
            duration_ms = max(200, min(60_000, duration_ms))
            asyncio.create_task(_pulse(device, duration_ms))
            log.info("demo pulse %s for %dms", entity_id, duration_ms)
            return web.json_response({
                "entity_id": entity_id,
                "action": "pulse",
                "duration_ms": duration_ms,
            })

        return web.json_response(
            {"error": f"unknown action: {action}"}, status=400,
        )

    async def floorplan(_request: web.Request) -> web.Response:
        # The GUI fetches this at startup to learn rooms + device
        # positions. Source of truth is the workdir copy if present,
        # falling back to the bundled package seed. Validated on every
        # read; bad JSON surfaces as a 500 rather than letting the GUI
        # crash on undefined fields.
        from ..floorplan import (
            load_floorplan, resolve_floorplan_path, FloorplanError,
        )

        path = resolve_floorplan_path()
        try:
            data = load_floorplan(path)
        except FileNotFoundError:
            return web.json_response(
                {"error": f"floorplan.json missing at {path}"},
                status=500,
            )
        except FloorplanError as exc:
            return web.json_response(
                {"error": f"floorplan.json invalid: {exc}"},
                status=500,
            )
        return web.json_response(data)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "devices": len(devices)})

    async def ha_websocket_proxy(request: web.Request) -> web.WebSocketResponse:
        """Pipe WebSocket frames between the floor-plan GUI and HA.

        The GUI used to connect directly to HA's WebSocket at the URL
        returned by /api/config (which is `localhost:8123` from the
        kit's perspective). That works for a same-machine install but
        breaks when the GUI is loaded through SSH or VS Code port
        forwarding from a different host: ``localhost`` in the browser
        then refers to the user's own machine, and HA's port may be
        remapped or not forwarded at all.

        Proxying through the control server's own port (the one the
        GUI loaded from) means the GUI can use a same-origin
        WebSocket URL that always resolves correctly regardless of
        how the user reached the page.

        Implementation is a transparent passthrough — frames are
        forwarded both ways unchanged, including the auth handshake.
        """
        ha_url = os.environ.get("HA_URL", "http://localhost:8123")
        ws_url = ha_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        ws_url += "/api/websocket"

        client_ws = web.WebSocketResponse()
        await client_ws.prepare(request)
        log.info("HA WS proxy: client connected, dialing %s", ws_url)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ha_ws:
                    async def client_to_ha() -> None:
                        async for msg in client_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await ha_ws.send_str(msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                return

                    async def ha_to_client() -> None:
                        async for msg in ha_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await client_ws.send_str(msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                return

                    await asyncio.gather(
                        client_to_ha(), ha_to_client(), return_exceptions=True,
                    )
        except Exception:
            log.exception("HA WS proxy failed")
        finally:
            if not client_ws.closed:
                await client_ws.close()
            log.info("HA WS proxy: client disconnected")
        return client_ws

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/config", config)
    app.router.add_get("/api/floorplan", floorplan)
    app.router.add_post("/api/demo/trigger", demo_trigger)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/ha/websocket", ha_websocket_proxy)
    # Static fallback for any other gui/* file (CSS/JS if we split out).
    if GUI_DIR.is_dir():
        app.router.add_static("/static/", GUI_DIR, show_index=False)
    # User-supplied backdrop images live in <workdir>/images/. Mounted
    # at /images/ so floorplan.json's `backdrop` field is just a
    # filename — no path traversal, no leaking the workdir layout.
    from ..floorplan import resolve_images_dir
    images = resolve_images_dir()
    if images is not None:
        app.router.add_static("/images/", images, show_index=False)
    return app


async def _pulse(device: BinarySensor, duration_ms: int) -> None:
    """Fire ON, sleep, fire OFF. For motion / leak / smoke pulses."""
    try:
        await device.set(True)
        await asyncio.sleep(duration_ms / 1000.0)
        await device.set(False)
    except Exception:
        log.exception("pulse for %s failed", device.unique_id)


async def run_control_server(
    devices: List[Device],
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Start the control HTTP server. Returns when stop_event is set."""
    app = _build_app(devices)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(
        "Control server listening on http://%s:%d (gui dir: %s)",
        host, port, GUI_DIR,
    )
    try:
        if stop_event is not None:
            await stop_event.wait()
        else:
            # No stop signal — block forever (Ctrl-C / SIGTERM cancels
            # the surrounding asyncio.gather).
            await asyncio.Event().wait()
    finally:
        await runner.cleanup()
