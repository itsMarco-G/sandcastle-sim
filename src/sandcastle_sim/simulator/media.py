"""Simulated speaker — on/off + volume only, no audio.

HA's MQTT integration doesn't have a first-class media_player domain
the way it does for lights and locks, so we use the generic
abbreviated-config mqtt media_player.

For the demo we expose the minimum the contract needs: on/off (state)
and volume_level (0.0–1.0). Play/pause/next live in milestone 7's
control tools.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from .base import Device

log = logging.getLogger(__name__)


class MediaPlayer(Device):
    domain = "media_player"

    def __init__(self, mqtt, spec: Dict[str, Any]) -> None:
        super().__init__(mqtt, spec)
        self.state = {
            "state": "off",
            "volume_level": 0.3,
        }

    def discovery_extras(self) -> Dict[str, Any]:
        # HA's MQTT media_player is sparse; we keep this lightweight
        # for milestone 4 — just enough to register the entity. Full
        # play/pause/next wiring is milestone 7.
        return {
            "command_topic": self.command_topic,
            "state_topic": self.state_topic,
            "value_template": "{{ value_json.state }}",
        }

    async def handle_command(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="ignore").strip().lower()
        if text in ("on", "play"):
            self.state["state"] = "playing"
        elif text in ("off", "stop"):
            self.state["state"] = "off"
        elif text in ("pause",):
            self.state["state"] = "paused"
        else:
            log.warning("%s: unrecognised command %r", self.unique_id, text)
            return
        await self.publish_state()
        log.info("%s -> %s", self.unique_id, self.state["state"])
