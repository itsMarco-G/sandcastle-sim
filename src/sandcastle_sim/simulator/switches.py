"""Simulated switches — single-channel smart plugs.

The coffee-machine plug carries `watts_when_on` so the power-meter
behavior (milestone 7) can sum live wattage across active devices.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Device

log = logging.getLogger(__name__)


class Switch(Device):
    domain = "switch"

    # Plain-text state topic to match the simplest HA switch
    # contract: "ON" / "OFF" payloads, no JSON.
    PAYLOAD_ON = "ON"
    PAYLOAD_OFF = "OFF"

    def __init__(self, mqtt, spec: Dict[str, Any]) -> None:
        super().__init__(mqtt, spec)
        self.watts_when_on = float(spec.get("watts_when_on", 0.0))
        self.state = {"_text": self.PAYLOAD_OFF}

    def discovery_extras(self) -> Dict[str, Any]:
        return {
            "command_topic": self.command_topic,
            "payload_on": self.PAYLOAD_ON,
            "payload_off": self.PAYLOAD_OFF,
            "state_on": self.PAYLOAD_ON,
            "state_off": self.PAYLOAD_OFF,
            "optimistic": False,
        }

    async def publish_state(self) -> None:
        # Plain-text state topic, not JSON.
        await self.mqtt.publish(
            self.state_topic, self.state["_text"], retain=True,
        )

    @property
    def is_on(self) -> bool:
        return self.state["_text"] == self.PAYLOAD_ON

    async def handle_command(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="ignore").strip().upper()
        if text in (self.PAYLOAD_ON, self.PAYLOAD_OFF):
            self.state["_text"] = text
            await self.publish_state()
            log.info("%s -> %s", self.unique_id, text)
        else:
            log.warning("%s: unrecognised command %r", self.unique_id, text)
