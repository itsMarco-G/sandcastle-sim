"""Significant-event classifier + rolling buffer.

Mirrors the GUI's EVENT_KINDS in spirit, but explicitly excludes
motion — motion sensors fire often enough that surfacing every pulse
to the agent would be noise rather than signal. The GUI still shows
motion in its event panel because it's a useful visual cue.

Significant events tracked:

  contact_open   — door/window binary_sensor flips on
  contact_close  — door/window binary_sensor flips off
  leak           — moisture binary_sensor flips on
  smoke          — smoke binary_sensor flips on
  lock_changed   — any lock state transition
  vacuum_state   — any vacuum state transition

Each event is a flat dict matching the contract in
``docs/tool-contract.md`` §3.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULT_MAX_EVENTS = 50


def classify(
    new_state: Optional[Dict[str, Any]],
    old_state: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Classify an HA state-change pair as one of our significant kinds.

    Returns the kind label or None if the transition isn't
    significant. Motion (device_class=='motion') is intentionally
    skipped here — the agent shouldn't react to ambient motion in
    the demo; the GUI continues to render motion events visually
    via its own filter.
    """
    if not new_state:
        return None
    entity_id = new_state.get("entity_id", "")
    new = new_state.get("state")
    old = old_state.get("state") if old_state else None
    if new == old:
        return None

    if entity_id.startswith("lock."):
        return "lock_changed"
    if entity_id.startswith("vacuum."):
        return "vacuum_state"

    attrs = new_state.get("attributes") or {}
    dc = attrs.get("device_class")

    if new == "on" and old != "on":
        if dc == "moisture":
            return "leak"
        if dc == "smoke":
            return "smoke"
        if dc in ("door", "window"):
            return "contact_open"
        # Motion deliberately skipped.
    elif new == "off" and old == "on":
        if dc in ("door", "window"):
            return "contact_close"

    return None


def format_event(
    kind: str,
    new_state: Dict[str, Any],
    old_state: Optional[Dict[str, Any]],
    area: Optional[str],
) -> Dict[str, Any]:
    """Shape a classified transition into the contract event payload."""
    attrs = new_state.get("attributes") or {}
    return {
        "kind": kind,
        "entity_id": new_state.get("entity_id"),
        "friendly_name": attrs.get("friendly_name"),
        "area": area,
        "state": new_state.get("state"),
        "previous_state": old_state.get("state") if old_state else None,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


class EventBuffer:
    """Thread-safe (asyncio-safe) rolling buffer of recent significant events.

    Also acts as a pub-sub: subscribers register an asyncio.Queue and
    receive every event added to the buffer. Used by the SSE endpoint
    on the MCP server to push events to consumers (the home_agent's
    smart-home registrar) in real time, no polling.
    """

    def __init__(self, maxlen: int = DEFAULT_MAX_EVENTS) -> None:
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = asyncio.Lock()
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber queue and return it.

        Caller is responsible for draining the queue and calling
        ``unsubscribe`` when done. A bounded queue would back up the
        producer if a consumer is slow; for the demo we use unbounded
        (events are sparse and small).
        """
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def add(self, event: Dict[str, Any]) -> None:
        async with self._lock:
            self._buffer.append(event)
        log.info(
            "home_event %s %s state=%s prev=%s",
            event["kind"], event["entity_id"], event["state"],
            event.get("previous_state"),
        )
        # Fan-out to live subscribers. put_nowait is safe because the
        # queues are unbounded; if we ever switch to bounded queues
        # we'd need to handle Full here.
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                log.exception("event subscriber push failed; dropping")

    async def list(
        self,
        limit: int = 10,
        since: Optional[str] = None,
        kinds: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        async with self._lock:
            events = list(self._buffer)
        if since:
            events = [e for e in events if e["timestamp"] > since]
        if kinds:
            kinds_set = set(kinds)
            events = [e for e in events if e["kind"] in kinds_set]
        events.reverse()  # newest first
        if limit and limit > 0:
            events = events[:limit]
        return events

    def __len__(self) -> int:
        return len(self._buffer)
