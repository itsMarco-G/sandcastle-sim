"""Simulated read-only sensors — Sensor (numeric) and BinarySensor.

Both publish plain text on their state topic. Sensors carry a
device_class + unit (temperature/°C, power/W) so HA renders the
right icon. Binary sensors flip ON/OFF; values shaped for HA's
default `payload_on=ON, payload_off=OFF` contract.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Device

log = logging.getLogger(__name__)


class Sensor(Device):
    """Generic HA sensor — numeric or string-valued.

    The shape adapts based on the topology's ``initial`` field:
    a number stays numeric (state_class=measurement, formatted as a
    decimal), anything else publishes the raw string. Useful for
    flag-style sensors like ``sensor.vacuum_current_room``.
    """

    domain = "sensor"

    def __init__(self, mqtt, spec: Dict[str, Any]) -> None:
        super().__init__(mqtt, spec)
        self.device_class = spec.get("device_class")
        self.unit = spec.get("unit")
        initial = spec.get("initial", 0.0)
        self._is_numeric = isinstance(initial, (int, float))
        if self._is_numeric:
            self.value: Any = float(initial)
        else:
            self.value = str(initial)
        self.state = {"_value": self.value}

    def discovery_extras(self) -> Dict[str, Any]:
        extras: Dict[str, Any] = {}
        if self.device_class:
            extras["device_class"] = self.device_class
        if self.unit:
            extras["unit_of_measurement"] = self.unit
        # state_class only applies to numeric sensors; string sensors
        # use the implicit text mode.
        if self._is_numeric:
            extras["state_class"] = "measurement"
        return extras

    async def publish_state(self) -> None:
        if self._is_numeric:
            text = f"{self.value:g}"
        else:
            text = str(self.value)
        await self.mqtt.publish(self.state_topic, text, retain=True)

    async def set_value(self, new_value: Any) -> None:
        if self._is_numeric:
            self.value = float(new_value)
        else:
            self.value = str(new_value)
        self.state["_value"] = self.value
        await self.publish_state()


class BinarySensor(Device):
    domain = "binary_sensor"

    PAYLOAD_ON = "ON"
    PAYLOAD_OFF = "OFF"

    def __init__(self, mqtt, spec: Dict[str, Any]) -> None:
        super().__init__(mqtt, spec)
        self.device_class = spec.get("device_class")
        self.state = {"_text": self.PAYLOAD_OFF}

    def discovery_extras(self) -> Dict[str, Any]:
        extras = {
            "payload_on": self.PAYLOAD_ON,
            "payload_off": self.PAYLOAD_OFF,
        }
        if self.device_class:
            extras["device_class"] = self.device_class
        return extras

    async def publish_state(self) -> None:
        await self.mqtt.publish(self.state_topic, self.state["_text"], retain=True)

    @property
    def is_on(self) -> bool:
        return self.state["_text"] == self.PAYLOAD_ON

    async def set(self, on: bool) -> None:
        new_text = self.PAYLOAD_ON if on else self.PAYLOAD_OFF
        if new_text == self.state["_text"]:
            return
        self.state["_text"] = new_text
        await self.publish_state()
        log.info("%s -> %s", self.unique_id, new_text)
