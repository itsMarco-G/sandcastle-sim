"""Simulated covers — blinds with a 0–100 position slider.

Position is stored as an integer percentage (0 closed, 100 open),
matching the contract. We publish position via a JSON state topic
to keep things consistent with lights; HA's `value_template` handles
the parse.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from .base import Device

log = logging.getLogger(__name__)


class Cover(Device):
    domain = "cover"

    def __init__(self, mqtt, spec: Dict[str, Any]) -> None:
        super().__init__(mqtt, spec)
        self.state = {
            "state": "closed",
            "position": 0,
        }

    def discovery_extras(self) -> Dict[str, Any]:
        # HA requires `position_topic` whenever `set_position_topic`
        # is set. We point both state and position at the same JSON
        # state topic and pull each field via templates.
        return {
            "command_topic": self.command_topic,
            "set_position_topic": self.command_topic,
            "position_topic": self.state_topic,
            "value_template": "{{ value_json.state }}",
            "position_template": "{{ value_json.position }}",
            "state_open": "open",
            "state_closed": "closed",
            "state_opening": "opening",
            "state_closing": "closing",
            "payload_open": "OPEN",
            "payload_close": "CLOSE",
            "payload_stop": "STOP",
            "position_open": 100,
            "position_closed": 0,
            "optimistic": False,
        }

    async def handle_command(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="ignore").strip()
        # set_position_topic + command_topic share the same MQTT
        # topic. HA sends a numeric position string for the position
        # set; OPEN/CLOSE/STOP for state changes.
        if text == "OPEN":
            self.state = {"state": "open", "position": 100}
        elif text == "CLOSE":
            self.state = {"state": "closed", "position": 0}
        elif text == "STOP":
            # Real blinds stop in place; simulator just keeps last
            # position but flips state to match.
            pos = self.state.get("position", 0)
            self.state = {
                "state": "open" if pos > 0 else "closed",
                "position": pos,
            }
        else:
            try:
                pos = max(0, min(100, int(text)))
            except ValueError:
                log.warning("%s: unrecognised command %r", self.unique_id, text)
                return
            self.state = {
                "state": "open" if pos > 0 else "closed",
                "position": pos,
            }
        await self.publish_state()
        log.info(
            "%s -> state=%s position=%d",
            self.unique_id,
            self.state["state"],
            self.state["position"],
        )
