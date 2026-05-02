"""Simulated lights — dimmable (brightness only) and RGB.

JSON schema (HA's `schema: json` mode) lets us keep one command and
one state topic per light. The model is the canonical
``schema=json`` payload format documented at
https://www.home-assistant.io/integrations/light.mqtt/#json-schema.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from .base import Device

log = logging.getLogger(__name__)


class Light(Device):
    domain = "light"

    def __init__(self, mqtt, spec: Dict[str, Any]) -> None:
        super().__init__(mqtt, spec)
        self.kind = spec.get("kind", "dimmable")  # "dimmable" or "rgb"
        # Initial state: off, but with an idle brightness so a "turn
        # on" with no brightness lands at a sensible level.
        self.state = {
            "state": "OFF",
            "brightness": 128,
        }
        if self.kind == "rgb":
            self.state["color"] = {"r": 255, "g": 255, "b": 255}
            self.state["color_mode"] = "rgb"
        else:
            self.state["color_mode"] = "brightness"

    def discovery_extras(self) -> Dict[str, Any]:
        modes = ["rgb"] if self.kind == "rgb" else ["brightness"]
        return {
            "schema": "json",
            "command_topic": self.command_topic,
            "brightness": True,
            "supported_color_modes": modes,
            # Cap brightness at HA's default 0–255 range.
            "brightness_scale": 255,
        }

    async def handle_command(self, payload: bytes) -> None:
        try:
            cmd = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("%s: invalid JSON command %r", self.unique_id, payload[:120])
            return

        # `state` field accepts "ON" / "OFF" (HA convention). We
        # normalise to upper-case for state publish so HA recognises
        # the value.
        if "state" in cmd:
            self.state["state"] = str(cmd["state"]).upper()

        if "brightness" in cmd:
            try:
                b = int(cmd["brightness"])
            except (TypeError, ValueError):
                b = self.state["brightness"]
            self.state["brightness"] = max(0, min(255, b))
            # Setting brightness implies the light is on (HA's
            # convention — sending brightness alone turns the light
            # on at that level).
            self.state["state"] = "ON"

        if self.kind == "rgb" and "color" in cmd:
            color = cmd["color"]
            if isinstance(color, dict):
                self.state["color"] = {
                    "r": int(color.get("r", 0)),
                    "g": int(color.get("g", 0)),
                    "b": int(color.get("b", 0)),
                }
                self.state["color_mode"] = "rgb"
                self.state["state"] = "ON"

        await self.publish_state()
        log.info(
            "%s -> state=%s brightness=%s%s",
            self.unique_id,
            self.state["state"],
            self.state["brightness"],
            f" color={self.state['color']}" if self.kind == "rgb" else "",
        )
