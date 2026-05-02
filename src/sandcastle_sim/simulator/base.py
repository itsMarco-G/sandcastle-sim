"""Base Device class and shared discovery payload helpers.

The simulator pattern: every device subclass owns its `state` dict
(domain-specific shape), implements `discovery_extras()` to add the
domain-specific fields HA needs in the discovery payload, and
optionally implements `handle_command(payload)` to react to MQTT
commands published to its `command_topic`.

The base class handles:

* Topic naming (`homeassistant/<domain>/sim_<slug>/{config,state,set}`)
* Discovery payload assembly (with `device.suggested_area` so HA
  auto-assigns the right area_id)
* Publish helpers (discovery + state)

Subclasses override `domain`, `state` (initialised in __init__), and
`discovery_extras` / `handle_command` as needed.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import aiomqtt

from .topology import AREA_NAMES

log = logging.getLogger(__name__)

# All retained MQTT messages from the simulator land under this
# prefix. Matches HA's default discovery_prefix.
DISCOVERY_PREFIX = "homeassistant"


class Device:
    """Base class for all simulated devices."""

    domain: str = "device"  # subclasses override

    def __init__(self, mqtt: aiomqtt.Client, spec: Dict[str, Any]) -> None:
        self.mqtt = mqtt
        self.slug: str = spec["slug"]
        self.name: str = spec["name"]
        self.area: Optional[str] = spec.get("area")
        self._spec = spec
        # Subclasses must set self.state to a JSON-serialisable dict
        # before publish_state() is called.
        self.state: Dict[str, Any] = {}

    # ---- topics ----------------------------------------------------- #

    @property
    def unique_id(self) -> str:
        # Prefix with `sim_` so any MQTT-discovered entity that's part
        # of the simulator can be told apart from a real device by
        # its unique_id alone — useful when debugging.
        return f"sim_{self.domain}_{self.slug}"

    @property
    def base_topic(self) -> str:
        return f"{DISCOVERY_PREFIX}/{self.domain}/sim_{self.slug}"

    @property
    def config_topic(self) -> str:
        return f"{self.base_topic}/config"

    @property
    def state_topic(self) -> str:
        return f"{self.base_topic}/state"

    @property
    def command_topic(self) -> str:
        return f"{self.base_topic}/set"

    # ---- discovery -------------------------------------------------- #

    def device_info(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "identifiers": [self.unique_id],
            "name": self.name,
            "manufacturer": "Smart Home Demo",
            "model": f"Simulated {self.domain}",
            "sw_version": "0.1.0",
        }
        # `suggested_area` accepts the friendly area name; HA matches
        # case-insensitively against the area registry.
        if self.area:
            info["suggested_area"] = AREA_NAMES.get(self.area, self.area)
        return info

    def discovery_extras(self) -> Dict[str, Any]:
        """Domain-specific fields added to the discovery payload.

        Subclasses fill in things like `command_topic`, `schema`,
        `supported_color_modes`, `device_class`, etc.
        """
        return {}

    def discovery_payload(self) -> Dict[str, Any]:
        # Modern HA convention for "one entity per device": leave the
        # entity-level `name` null so friendly_name comes from
        # `device.name` alone (otherwise HA concatenates them and you
        # end up with "Kitchen Light Kitchen Light").
        #
        # We rely on HA's slugify(device.name) for entity_id
        # derivation rather than `object_id`. `object_id` is honored
        # only on first-creation for some MQTT-discoverable platforms
        # and is ignored once an entity exists in the registry, so
        # picking `device.name` values that slugify to the contract's
        # entity_id slugs is the reliable path. Topology names are
        # chosen with that in mind.
        return {
            "name": None,
            "unique_id": self.unique_id,
            "state_topic": self.state_topic,
            **self.discovery_extras(),
            "device": self.device_info(),
        }

    # ---- publish helpers ------------------------------------------- #

    async def publish_discovery(self) -> None:
        payload = json.dumps(self.discovery_payload())
        await self.mqtt.publish(self.config_topic, payload, retain=True)

    async def publish_state(self) -> None:
        """Publish current state. Default is JSON; subclasses can
        override for plain-text-state domains (e.g. lock, vacuum)."""
        await self.mqtt.publish(
            self.state_topic, json.dumps(self.state), retain=True,
        )

    # ---- command handling ------------------------------------------ #

    async def handle_command(self, payload: bytes) -> None:
        """React to a command on `command_topic`. Default no-op.

        Subclasses override. Must update self.state in place and
        call `await self.publish_state()` to broadcast the change.
        """
        log.debug("%s ignored command %r", self.unique_id, payload[:120])

    # ---- lifecycle -------------------------------------------------- #

    def has_command_topic(self) -> bool:
        """Subclasses with a command_topic in their discovery payload
        return True so the dispatcher subscribes to that topic.
        """
        return "command_topic" in self.discovery_extras()
