"""Simulated robot vacuum — dock / start / stop + current_room attr.

The full HA MQTT vacuum schema (state + JSON command) is overkill
for the demo. We use HA's `mqtt.vacuum` legacy schema which is
plain-text:

    payload_start  -> "cleaning"
    payload_stop   -> "idle"
    payload_return -> "returning"

Plus a JSON state topic that exposes current_room so the contract's
``vacuum_control`` tool can return it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from .base import Device

log = logging.getLogger(__name__)


class Vacuum(Device):
    domain = "vacuum"

    PAYLOAD_START = "start"
    PAYLOAD_STOP = "stop"
    PAYLOAD_PAUSE = "pause"
    PAYLOAD_RETURN = "return_to_base"

    STATE_DOCKED = "docked"
    STATE_CLEANING = "cleaning"
    STATE_RETURNING = "returning"
    STATE_PAUSED = "paused"
    STATE_IDLE = "idle"

    def __init__(self, mqtt, spec: Dict[str, Any]) -> None:
        super().__init__(mqtt, spec)
        self.state = {
            "state": self.STATE_DOCKED,
            "battery_level": 100,
            "current_room": None,  # set when cleaning starts
        }

    def discovery_extras(self) -> Dict[str, Any]:
        # HA's mqtt.vacuum schema accepts only a fixed list of
        # supported_features values (see MQTT_VACUUM_FEATURES in HA's
        # source). "battery" isn't one — battery_level is reported
        # via the JSON state payload instead.
        return {
            "schema": "state",
            "command_topic": self.command_topic,
            "supported_features": [
                "start", "stop", "pause", "return_home", "status",
            ],
            "payload_start": self.PAYLOAD_START,
            "payload_stop": self.PAYLOAD_STOP,
            "payload_pause": self.PAYLOAD_PAUSE,
            "payload_return_to_base": self.PAYLOAD_RETURN,
        }

    async def handle_command(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="ignore").strip()
        if text == self.PAYLOAD_START:
            self.state["state"] = self.STATE_CLEANING
            self.state.setdefault("current_room", "living_room")
        elif text == self.PAYLOAD_STOP:
            self.state["state"] = self.STATE_IDLE
        elif text == self.PAYLOAD_PAUSE:
            self.state["state"] = self.STATE_PAUSED
        elif text == self.PAYLOAD_RETURN:
            self.state["state"] = self.STATE_RETURNING
            self.state["current_room"] = None
        else:
            log.warning("%s: unrecognised command %r", self.unique_id, text)
            return
        await self.publish_state()
        log.info("%s -> %s", self.unique_id, self.state["state"])

    async def set_room(self, area_key: Optional[str]) -> None:
        self.state["current_room"] = area_key
        await self.publish_state()
