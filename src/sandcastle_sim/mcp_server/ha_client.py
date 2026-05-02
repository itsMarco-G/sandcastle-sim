"""Async Home Assistant WebSocket client.

A single persistent connection multiplexed by message ID. Handles
auth, request/response correlation, and exposes the registry +
state queries the MCP tools need.

Server-initiated events (subscribed-to state changes) are routed to
a callback if registered, otherwise dropped. The MCP server uses
this for `notifications/home_event` push in milestone 8 — for now
the event hook is a no-op.

Designed to be used as a long-lived singleton inside the MCP server
process. One client, one connection, many concurrent tool calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

# Optional callback type for server-initiated events.
EventCallback = Callable[[Dict[str, Any]], Awaitable[None]]


def _http_to_ws(http_url: str) -> str:
    """Map http(s)://host:port[/...] -> ws(s)://host:port/api/websocket."""
    parsed = urlparse(http_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    netloc = parsed.netloc or parsed.path  # tolerate http://host without /
    return f"{scheme}://{netloc}/api/websocket"


class HAClient:
    """Persistent HA WebSocket client with request/response multiplexing."""

    def __init__(self, http_url: str, token: str) -> None:
        self._url = _http_to_ws(http_url)
        self._token = token
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future[Dict[str, Any]]] = {}
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()  # serialises sends
        self._on_event: Optional[EventCallback] = None
        self._connected = asyncio.Event()

    # ---- lifecycle -------------------------------------------------- #

    async def connect(self) -> None:
        """Open the WS, authenticate, start the reader task."""
        log.info("Connecting to HA WebSocket at %s", self._url)
        self._ws = await websockets.connect(self._url, max_size=4 * 1024 * 1024)

        hello = json.loads(await self._ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected first WS msg: {hello!r}")

        await self._ws.send(json.dumps({
            "type": "auth",
            "access_token": self._token,
        }))
        ack = json.loads(await self._ws.recv())
        if ack.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth failed: {ack!r}")

        log.info("HA WS authenticated (HA version %s)", ack.get("ha_version"))
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="ha-ws-reader"
        )
        self._connected.set()

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._connected.clear()
        # Fail any pending requests so callers don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionClosed(None, None))
        self._pending.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def set_event_handler(self, cb: Optional[EventCallback]) -> None:
        """Register/clear a callback for server-initiated event messages.

        ``cb`` receives raw HA event payloads of type=event. It must
        not raise — exceptions are logged and dropped.
        """
        self._on_event = cb

    # ---- request/response ------------------------------------------- #

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("HA WS sent non-JSON message: %r", raw[:200])
                    continue

                mtype = msg.get("type")
                mid = msg.get("id")

                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                    continue

                if mtype == "event" and self._on_event is not None:
                    try:
                        await self._on_event(msg)
                    except Exception:
                        log.exception("event handler raised; continuing")
                    continue

                # Unsolicited / unknown — dropped silently. HA sends
                # `pong` and `result` (when subscription ack arrives
                # late), neither matters here.
        except ConnectionClosed:
            log.warning("HA WS connection closed")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("HA WS reader crashed")
        finally:
            self._connected.clear()

    async def _call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send a request keyed by an auto-generated id, await response.

        Raises ``RuntimeError`` if the response indicates failure.
        """
        if self._ws is None:
            raise RuntimeError("HAClient.connect() not called")

        async with self._lock:
            self._next_id += 1
            mid = self._next_id

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending[mid] = fut

        body = {**payload, "id": mid}
        try:
            await self._ws.send(json.dumps(body))
        except Exception:
            self._pending.pop(mid, None)
            raise

        resp = await fut
        if not resp.get("success", True):
            err = resp.get("error", {})
            raise RuntimeError(
                f"HA WS error ({err.get('code', '?')}): "
                f"{err.get('message', resp)!r}"
            )
        return resp

    # ---- public queries --------------------------------------------- #

    async def list_areas(self) -> List[Dict[str, Any]]:
        resp = await self._call({"type": "config/area_registry/list"})
        return list(resp.get("result", []))

    async def list_entities(self) -> List[Dict[str, Any]]:
        resp = await self._call({"type": "config/entity_registry/list"})
        return list(resp.get("result", []))

    async def list_devices_registry(self) -> List[Dict[str, Any]]:
        resp = await self._call({"type": "config/device_registry/list"})
        return list(resp.get("result", []))

    async def get_states(self) -> List[Dict[str, Any]]:
        resp = await self._call({"type": "get_states"})
        return list(resp.get("result", []))

    async def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        states = await self.get_states()
        for s in states:
            if s.get("entity_id") == entity_id:
                return s
        return None

    # ---- service calls --------------------------------------------- #

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: Optional[Dict[str, Any]] = None,
        target: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Invoke `domain.service` via HA's WS service registry.

        Mirrors HA's REST `/api/services/<domain>/<service>` shape.
        Returns the WS result envelope; the resulting entity state
        must be re-fetched via `get_state` if the caller needs it.
        """
        body: Dict[str, Any] = {
            "type": "call_service",
            "domain": domain,
            "service": service,
        }
        if service_data:
            body["service_data"] = service_data
        if target:
            body["target"] = target
        return await self._call(body)

    # ---- subscription helpers (used in milestone 8) ----------------- #

    async def subscribe_state_changed(self) -> int:
        """Subscribe to state_changed events. Returns subscription id.

        After this resolves, all state_changed events are delivered
        through the registered event handler.
        """
        resp = await self._call({
            "type": "subscribe_events",
            "event_type": "state_changed",
        })
        return int(resp.get("id", 0))
