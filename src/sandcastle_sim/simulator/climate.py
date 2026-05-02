"""Simulated thermostat — single whole-home unit.

Tracks current_temperature, target_temperature, and hvac_mode. The
JSON state shape matches HA's `mqtt.climate` JSON-schema mode; we
expose discrete topics for clarity.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from .base import Device

log = logging.getLogger(__name__)


class Climate(Device):
    domain = "climate"

    DEFAULT_TARGET = 20.0
    DEFAULT_CURRENT = 19.5
    MODES = ["off", "heat", "cool", "auto"]

    def __init__(self, mqtt, spec: Dict[str, Any]) -> None:
        super().__init__(mqtt, spec)
        self.state = {
            "current_temperature": self.DEFAULT_CURRENT,
            "target_temperature": self.DEFAULT_TARGET,
            "hvac_mode": "auto",
        }

    @property
    def mode_command_topic(self) -> str:
        return f"{self.base_topic}/mode/set"

    @property
    def temp_command_topic(self) -> str:
        return f"{self.base_topic}/temperature/set"

    @property
    def current_temp_topic(self) -> str:
        return f"{self.base_topic}/current_temperature"

    def discovery_extras(self) -> Dict[str, Any]:
        return {
            "modes": self.MODES,
            "mode_state_topic": self.state_topic,
            "mode_state_template": "{{ value_json.hvac_mode }}",
            "mode_command_topic": self.mode_command_topic,
            "temperature_state_topic": self.state_topic,
            "temperature_state_template": "{{ value_json.target_temperature }}",
            "temperature_command_topic": self.temp_command_topic,
            "current_temperature_topic": self.current_temp_topic,
            "min_temp": 7,
            "max_temp": 35,
            "temp_step": 0.5,
            "temperature_unit": "C",
        }

    def has_command_topic(self) -> bool:
        # Climate uses two command topics — register both for
        # subscription via override below.
        return True

    def command_topics(self) -> list[str]:
        return [self.mode_command_topic, self.temp_command_topic]

    async def publish_discovery(self) -> None:
        # Override to also publish initial current_temperature; HA's
        # current_temperature_topic is separate from state_topic.
        await super().publish_discovery()

    async def publish_state(self) -> None:
        await self.mqtt.publish(
            self.state_topic, json.dumps(self.state), retain=True,
        )
        await self.mqtt.publish(
            self.current_temp_topic,
            str(self.state["current_temperature"]),
            retain=True,
        )

    async def handle_command(self, payload: bytes, topic: str = "") -> None:
        text = payload.decode("utf-8", errors="ignore").strip()
        if topic.endswith("/mode/set"):
            if text in self.MODES:
                self.state["hvac_mode"] = text
                log.info("%s -> mode=%s", self.unique_id, text)
            else:
                log.warning("%s: unknown mode %r", self.unique_id, text)
        elif topic.endswith("/temperature/set"):
            try:
                self.state["target_temperature"] = float(text)
                log.info("%s -> target=%s", self.unique_id, text)
            except ValueError:
                log.warning("%s: bad temperature %r", self.unique_id, text)
        await self.publish_state()
