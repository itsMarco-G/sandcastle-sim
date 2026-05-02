"""Simulated lock — payload_lock / payload_unlock plain-text contract."""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Device

log = logging.getLogger(__name__)


class Lock(Device):
    domain = "lock"

    PAYLOAD_LOCK = "LOCK"
    PAYLOAD_UNLOCK = "UNLOCK"
    STATE_LOCKED = "LOCKED"
    STATE_UNLOCKED = "UNLOCKED"

    def __init__(self, mqtt, spec: Dict[str, Any]) -> None:
        super().__init__(mqtt, spec)
        # Default to locked — the demo's "front door" should start
        # secured.
        self.state = {"_text": self.STATE_LOCKED}

    def discovery_extras(self) -> Dict[str, Any]:
        return {
            "command_topic": self.command_topic,
            "payload_lock": self.PAYLOAD_LOCK,
            "payload_unlock": self.PAYLOAD_UNLOCK,
            "state_locked": self.STATE_LOCKED,
            "state_unlocked": self.STATE_UNLOCKED,
            "optimistic": False,
        }

    async def publish_state(self) -> None:
        await self.mqtt.publish(
            self.state_topic, self.state["_text"], retain=True,
        )

    async def handle_command(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="ignore").strip().upper()
        if text == self.PAYLOAD_LOCK:
            self.state["_text"] = self.STATE_LOCKED
            await self.publish_state()
            log.info("%s -> LOCKED", self.unique_id)
        elif text == self.PAYLOAD_UNLOCK:
            self.state["_text"] = self.STATE_UNLOCKED
            await self.publish_state()
            log.info("%s -> UNLOCKED", self.unique_id)
        else:
            log.warning("%s: unrecognised command %r", self.unique_id, text)
