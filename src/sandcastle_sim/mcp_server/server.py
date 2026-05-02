"""Smart-home MCP server.

FastMCP over streamable HTTP. Exposes the discovery tools from the
v0.1 contract (``list_areas``, ``list_devices``, ``get_device_state``);
control + event tools land in milestones 5 and 8.

Reads ``HA_URL`` and ``HA_TOKEN`` from the environment. The Makefile's
``run-mcp`` target sources ``.env`` first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import StreamingResponse

from .events import EventBuffer, classify, format_event
from .ha_client import HAClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("sandcastle_sim.mcp_server")

# Domains we surface as "devices" in the contract. Excludes HA
# infrastructure entities (sun, zone, weather, conversation, tts,
# stt, person, todo, ...).
DEVICE_DOMAINS = {
    "light",
    "switch",
    "lock",
    "cover",
    "climate",
    "sensor",
    "binary_sensor",
    "media_player",
    "vacuum",
    "scene",
}

# Platforms (integrations) whose entities are HA infrastructure
# rather than user-controllable devices. The `default_config` bundle
# pulls these in (sun.sun, sensor.backup_*) and they'd otherwise
# clutter list_devices output. Add to this set if a future
# integration leaks more.
INFRA_PLATFORMS = {
    "sun",
    "backup",
    "weather",
    "tts",
    "stt",
    "conversation",
    "person",
    "zone",
    "home_assistant",
    "radio_browser",
    "analytics_insights",
    "system_log",
    "shopping_list",
    "todo",
    "schedule",
    "timer",
    "counter",
    "input_boolean",
    "input_button",
    "input_number",
    "input_select",
    "input_text",
}

# Entity attribute keys that are noisy / not useful to the LLM. We
# strip them from device payloads. Keep this list small — when in
# doubt include the attribute, the model can ignore it.
#
# `friendly_name` is dropped because we already surface it at the
# top level. `hs_color` / `xy_color` are dropped because they're
# redundant with `rgb_color` and just inflate the payload (each
# tuple is ~6 tokens). Every token we save here lowers Gemma's
# prompt-processing time on the next agent-loop iteration.
NOISY_ATTRS = {
    "supported_features",
    "editable",
    "icon",
    "entity_picture",
    "assumed_state",
    "restored",
    "device_class",  # we keep enough context elsewhere
    "friendly_name",
    "hs_color",
    "xy_color",
}

HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8765"))


# --------------------------------------------------------------------------- #
# Singleton HA client                                                         #
# --------------------------------------------------------------------------- #
# Lazily connect on first tool call rather than at process start so the
# MCP server boots even if HA is briefly unavailable. Reconnects on the
# next call after a drop.

_ha: Optional[HAClient] = None
_ha_lock = asyncio.Lock()

# Rolling buffer of significant events (motion excluded). Populated by
# the HA state_changed subscription set up in lifespan.
_events = EventBuffer()
_events_subscribed = False


async def _get_ha() -> HAClient:
    global _ha
    if _ha is not None and _ha.connected:
        return _ha
    async with _ha_lock:
        if _ha is not None and _ha.connected:
            return _ha
        if _ha is not None:
            await _ha.close()
        if not HA_TOKEN:
            raise RuntimeError(
                "HA_TOKEN env var is empty — run `sandcastle-sim bootstrap`"
            )
        client = HAClient(HA_URL, HA_TOKEN)
        await client.connect()
        _ha = client
        return _ha


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _resolve_area(
    entity_entry: Optional[Dict[str, Any]],
    devices_by_id: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    """Pick the entity's area: explicit area_id wins, else its device's."""
    if not entity_entry:
        return None
    explicit = entity_entry.get("area_id")
    if explicit:
        return explicit
    device_id = entity_entry.get("device_id")
    if device_id:
        device = devices_by_id.get(device_id)
        if device:
            return device.get("area_id")
    return None


def _derive_protocol(entity_entry: Optional[Dict[str, Any]]) -> str:
    """Pick a protocol label for the device based on integration."""
    if not entity_entry:
        return "unknown"
    platform = entity_entry.get("platform", "")
    if platform == "mqtt":
        return "mqtt"
    if platform == "matter":
        return "matter"
    return platform or "unknown"


def _strip_attrs(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Drop noisy keys, plus drop null values entirely.

    Off lights carry a pile of null fields (color_mode, brightness,
    etc.) that bloat the payload without conveying information. The
    model can infer "off" means no color/brightness from the
    top-level state field.
    """
    return {
        k: v for k, v in attrs.items()
        if k not in NOISY_ATTRS and v is not None
    }


def _format_device(
    state: Dict[str, Any],
    entities_by_id: Dict[str, Dict[str, Any]],
    devices_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Shape one HA state dict into the contract's device payload."""
    entity_id = state["entity_id"]
    domain = entity_id.split(".", 1)[0]
    entity_entry = entities_by_id.get(entity_id)
    attrs = state.get("attributes", {}) or {}
    return {
        "entity_id": entity_id,
        "friendly_name": attrs.get("friendly_name", entity_id),
        "domain": domain,
        "area": _resolve_area(entity_entry, devices_by_id),
        "state": state.get("state"),
        "attributes": _strip_attrs(attrs),
        "protocol": _derive_protocol(entity_entry),
    }


async def _gather_registries(ha: HAClient) -> tuple[
    List[Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
]:
    """Concurrent fetch of states + entity + device registries."""
    states, entities, devices = await asyncio.gather(
        ha.get_states(),
        ha.list_entities(),
        ha.list_devices_registry(),
    )
    entities_by_id = {e["entity_id"]: e for e in entities}
    devices_by_id = {d["id"]: d for d in devices}
    return states, entities_by_id, devices_by_id


# --------------------------------------------------------------------------- #
# FastMCP app + tools                                                         #
# --------------------------------------------------------------------------- #


async def _on_ha_event(msg: Dict[str, Any]) -> None:
    """HA WS event handler — classifies and buffers significant events.

    DO NOT call back into the HA WS from here (e.g. _gather_registries):
    the reader loop is single-threaded, so awaiting a fresh WS request
    deadlocks because the response can only land through the same loop
    we'd be blocking. Area resolution happens lazily inside
    list_recent_events when the buffer is read.
    """
    event = msg.get("event") or {}
    if event.get("event_type") != "state_changed":
        return
    data = event.get("data") or {}
    new_state = data.get("new_state")
    old_state = data.get("old_state")
    if not new_state:
        return
    kind = classify(new_state, old_state)
    if kind is None:
        return
    record = format_event(kind, new_state, old_state, area=None)
    await _events.add(record)


async def _ensure_events_subscribed() -> None:
    """Idempotent — subscribe to HA state_changed once per process."""
    global _events_subscribed
    if _events_subscribed:
        return
    ha = await _get_ha()
    ha.set_event_handler(_on_ha_event)
    await ha.subscribe_state_changed()
    _events_subscribed = True
    log.info("Subscribed to HA state_changed; buffering significant events.")


@asynccontextmanager
async def lifespan(_server: FastMCP):
    """Open the HA connection eagerly on first MCP session.

    FastMCP's streamable-HTTP transport calls this per-session, not
    per-process. We deliberately do NOT close the HA WS at session
    end — the next session would have to reconnect, which is wasted
    work when the same agent reconnects to MCP often. The HA
    connection lives for the process lifetime; the OS reaps it on
    shutdown.
    """
    try:
        await _get_ha()
        await _ensure_events_subscribed()
        log.info("HA WS ready + events subscribed.")
    except Exception as exc:
        log.warning(
            "HA not reachable on session start (%s); will retry on first call.",
            exc,
        )
    yield {}


mcp = FastMCP(
    "smart-home",
    host=MCP_HOST,
    port=MCP_PORT,
    lifespan=lifespan,
)


@mcp.tool()
async def list_areas() -> Dict[str, Any]:
    """Return every area (room) defined in the home.

    Call this when the user asks 'what rooms do I have' or you need
    to disambiguate which room a device is in. Cheap; safe to call
    eagerly.
    """
    ha = await _get_ha()
    areas = await ha.list_areas()
    return {
        "areas": [
            {"key": a["area_id"], "name": a["name"]}
            for a in sorted(areas, key=lambda x: x["area_id"])
        ]
    }


@mcp.tool()
async def list_devices(
    area: Optional[str] = None,
    domain: Optional[str] = None,
) -> Dict[str, Any]:
    """List the devices in the home.

    Use this whenever you need to know what exists, what state it's
    in, or which entity_id corresponds to a friendly name like
    'kitchen light'. Filter by ``area`` or ``domain`` to keep results
    focused. Always call this before a control tool if you're unsure
    of the exact entity_id.

    Areas in this home: ``living_room``, ``kitchen``, ``hallway``,
    ``bedroom`` (master), ``bedroom_2`` (second bedroom), ``bathroom``.

    IMPORTANT — generic plural requests:
    When the user says "the bedrooms", "bedroom lights", "all the
    bedroom lights", "all bedrooms", or similar plural forms, they
    almost always mean BOTH ``bedroom`` and ``bedroom_2``. For these
    queries, omit the ``area`` filter (or call this tool twice, once
    per area) so you find lights in both rooms, then act on them all.
    Only use a single area filter when the user specifies "the
    master bedroom" / "the main bedroom" (= ``bedroom``) or
    "bedroom 2" / "the spare bedroom" / "the kid's room" / "the
    second bedroom" (= ``bedroom_2``).
    """
    ha = await _get_ha()
    states, entities_by_id, devices_by_id = await _gather_registries(ha)

    out: List[Dict[str, Any]] = []
    for state in states:
        entity_id = state.get("entity_id", "")
        dom = entity_id.split(".", 1)[0]
        if dom not in DEVICE_DOMAINS:
            continue
        if domain and dom != domain:
            continue
        entity_entry = entities_by_id.get(entity_id)
        # Skip HA infrastructure (sun, backup, weather, ...) and
        # diagnostic/config sub-entities. Real user-facing devices
        # have entity_category == None and a platform that's a
        # real integration (mqtt, matter, etc.).
        if entity_entry is None:
            continue
        if entity_entry.get("entity_category"):
            continue
        if entity_entry.get("platform") in INFRA_PLATFORMS:
            continue
        device = _format_device(state, entities_by_id, devices_by_id)
        if area and device["area"] != area:
            continue
        out.append(device)

    out.sort(key=lambda d: d["entity_id"])
    return {"devices": out, "count": len(out)}


@mcp.tool()
async def get_device_state(entity_id: str) -> Dict[str, Any]:
    """Read the current state of a single device.

    Useful after a control action to confirm it took effect, or when
    the user asks about one specific thing. For multi-device queries
    use ``list_devices``.
    """
    if not entity_id:
        return {"error": "entity_id is required"}
    ha = await _get_ha()
    states, entities_by_id, devices_by_id = await _gather_registries(ha)
    for state in states:
        if state.get("entity_id") == entity_id:
            return _format_device(state, entities_by_id, devices_by_id)
    return {"error": f"Unknown entity_id: {entity_id}"}


# --------------------------------------------------------------------------- #
# Control tools                                                               #
# --------------------------------------------------------------------------- #
# Each control tool calls HA's service registry over WebSocket, waits a
# short beat for the state to settle (the simulator's command -> state
# round-trip is usually <30 ms), then reads back the resulting entity
# state so the caller can self-confirm. Returns the same shape as
# `get_device_state` on success, or `{"error": "..."}` on failure.

# Settling delay between issuing a service call and reading state.
# Tuned for the local-MQTT round-trip; bump if real devices appear.
_CONTROL_SETTLE_MS = 80


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


async def _post_control_state(ha, entity_id: str) -> Dict[str, Any]:
    """Re-read the entity after a control action and shape it."""
    await asyncio.sleep(_CONTROL_SETTLE_MS / 1000.0)
    states, entities_by_id, devices_by_id = await _gather_registries(ha)
    for state in states:
        if state.get("entity_id") == entity_id:
            return _format_device(state, entities_by_id, devices_by_id)
    return {"error": f"Entity disappeared after control: {entity_id}"}


@mcp.tool()
async def turn_on(
    entity_id: str,
    brightness: Optional[int] = None,
    rgb_color: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Turn on a single light or switch.

    Scope: ``light.*`` and ``switch.*`` ENTITIES ONLY. For other
    domains use the matching tool — picking ``turn_on`` for these
    is a mistake:
      * Thermostat (heating, cooling, temperature, "warmer",
        "cooler", "lowest setting") -> ``set_climate``
      * Locks ("lock", "unlock", "secure the door") -> ``lock`` /
        ``unlock``
      * Blinds / curtains / covers ("open the blind", "close the
        curtains") -> ``set_cover_position``
      * Vacuum ("start cleaning", "send the robot") ->
        ``vacuum_control``

    ``entity_id`` MUST be an exact ID such as ``light.kitchen_counter``
    or ``switch.coffee_machine`` — never a friendly name like
    "kitchen light". If you are not 100% sure of the entity_id, call
    ``list_devices`` first and pick the right one. Pass ONE entity
    per call; for multiple devices, call this tool once per entity.

    For lights, you can optionally set ``brightness`` (0-255) and
    ``rgb_color`` ([R, G, B], 0-255 each, RGB lights only).
    Returns the resulting state of the entity.
    """
    if not entity_id:
        return {"error": "entity_id is required"}
    domain = _domain(entity_id)
    if domain not in ("light", "switch"):
        return {
            "error": (
                f"turn_on supports only light and switch entities, "
                f"got {domain!r}"
            )
        }
    ha = await _get_ha()

    service_data: Dict[str, Any] = {"entity_id": entity_id}
    if domain == "light":
        if brightness is not None:
            try:
                service_data["brightness"] = max(0, min(255, int(brightness)))
            except (TypeError, ValueError):
                return {"error": f"brightness must be int 0-255, got {brightness!r}"}
        if rgb_color is not None:
            if (
                not isinstance(rgb_color, (list, tuple))
                or len(rgb_color) != 3
                or not all(isinstance(c, (int, float)) for c in rgb_color)
            ):
                return {"error": f"rgb_color must be [R, G, B] ints, got {rgb_color!r}"}
            service_data["rgb_color"] = [
                max(0, min(255, int(c))) for c in rgb_color
            ]
    elif (brightness is not None) or (rgb_color is not None):
        return {"error": "switches don't support brightness/rgb_color"}

    try:
        await ha.call_service(domain, "turn_on", service_data=service_data)
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, entity_id)


@mcp.tool()
async def turn_off(entity_id: str) -> Dict[str, Any]:
    """Turn off a light or switch.

    ``entity_id`` MUST be an exact ID such as ``light.kitchen_counter``,
    not a friendly name. If unsure, call ``list_devices`` first.

    Same domain rules as ``turn_on``: locks/blinds/thermostat use
    their own tools.
    """
    if not entity_id:
        return {"error": "entity_id is required"}
    domain = _domain(entity_id)
    if domain not in ("light", "switch"):
        return {
            "error": (
                f"turn_off supports only light and switch entities, "
                f"got {domain!r}"
            )
        }
    ha = await _get_ha()
    try:
        await ha.call_service(
            domain, "turn_off", service_data={"entity_id": entity_id}
        )
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, entity_id)


@mcp.tool()
async def lock(entity_id: str) -> Dict[str, Any]:
    """Lock a smart lock.

    ``entity_id`` MUST be an exact lock entity ID such as
    ``lock.front_door``. Use ``list_devices(domain='lock')`` if
    unsure. Returns the resulting state (``locked`` / ``locking`` /
    ``jammed``).
    """
    if not entity_id:
        return {"error": "entity_id is required"}
    if _domain(entity_id) != "lock":
        return {"error": f"lock requires a lock entity_id, got {entity_id!r}"}
    ha = await _get_ha()
    try:
        await ha.call_service(
            "lock", "lock", service_data={"entity_id": entity_id},
        )
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, entity_id)


@mcp.tool()
async def unlock(entity_id: str) -> Dict[str, Any]:
    """Unlock a smart lock.

    ``entity_id`` MUST be an exact lock entity ID. Confirm with the
    user first if the request is ambiguous — unlocking a door is a
    security-sensitive action.
    """
    if not entity_id:
        return {"error": "entity_id is required"}
    if _domain(entity_id) != "lock":
        return {"error": f"unlock requires a lock entity_id, got {entity_id!r}"}
    ha = await _get_ha()
    try:
        await ha.call_service(
            "lock", "unlock", service_data={"entity_id": entity_id},
        )
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, entity_id)


@mcp.tool()
async def set_cover_position(entity_id: str, position: int) -> Dict[str, Any]:
    """Move a blind / cover to a position from 0 (closed) to 100 (open).

    ``entity_id`` MUST be an exact cover entity ID. Animation takes
    a few seconds; the returned state may be ``opening`` /
    ``closing`` until the simulator finishes the move.
    """
    if not entity_id:
        return {"error": "entity_id is required"}
    if _domain(entity_id) != "cover":
        return {"error": f"set_cover_position requires a cover entity_id, got {entity_id!r}"}
    try:
        pos = max(0, min(100, int(position)))
    except (TypeError, ValueError):
        return {"error": f"position must be int 0-100, got {position!r}"}
    ha = await _get_ha()
    try:
        await ha.call_service(
            "cover", "set_cover_position",
            service_data={"entity_id": entity_id, "position": pos},
        )
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, entity_id)


@mcp.tool()
async def set_climate(
    entity_id: str,
    temperature: Optional[float] = None,
    hvac_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Set the thermostat — temperature (°C) and/or HVAC mode.

    THIS is the right tool for ANY temperature, heating, cooling, or
    HVAC-related request. Examples that all map here:
      * "set the temperature to 22"
      * "make it warmer / cooler / hotter / colder"
      * "lower the temperature to the lowest setting"
      * "turn on the heat" / "turn on the AC"
      * "set the thermostat to auto"

    Do NOT pick ``turn_on`` / ``turn_off`` for thermostat requests —
    those are for lights and switches only.

    ``entity_id`` is ``climate.home_thermostat`` for this demo.
    ``hvac_mode`` accepts ``heat`` / ``cool`` / ``auto`` / ``off``.
    Temperature range supported by this thermostat is 7-35 °C; for
    "lowest setting" pass ``temperature: 7``, for "highest" pass
    ``temperature: 35``. At least one of ``temperature`` /
    ``hvac_mode`` must be provided.
    """
    if not entity_id:
        return {"error": "entity_id is required"}
    if _domain(entity_id) != "climate":
        return {"error": f"set_climate requires a climate entity_id, got {entity_id!r}"}
    if temperature is None and hvac_mode is None:
        return {"error": "set_climate requires at least one of temperature or hvac_mode"}

    ha = await _get_ha()
    valid_modes = {"heat", "cool", "auto", "off"}

    try:
        if hvac_mode is not None:
            if hvac_mode not in valid_modes:
                return {"error": f"hvac_mode must be one of {sorted(valid_modes)}, got {hvac_mode!r}"}
            await ha.call_service(
                "climate", "set_hvac_mode",
                service_data={"entity_id": entity_id, "hvac_mode": hvac_mode},
            )
        if temperature is not None:
            try:
                temp = float(temperature)
            except (TypeError, ValueError):
                return {"error": f"temperature must be numeric, got {temperature!r}"}
            await ha.call_service(
                "climate", "set_temperature",
                service_data={"entity_id": entity_id, "temperature": temp},
            )
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, entity_id)


@mcp.tool()
async def vacuum_control(
    entity_id: str,
    action: str,
    area: Optional[str] = None,
) -> Dict[str, Any]:
    """Control the robot vacuum.

    ``entity_id`` is typically ``vacuum.robot_vacuum``. Actions:
    ``start``, ``pause``, ``stop``, ``return_to_base``,
    ``clean_room`` (also requires ``area``, e.g. ``"kitchen"``).
    The vacuum's current room is reported separately as
    ``sensor.vacuum_current_room`` because HA's MQTT vacuum
    integration doesn't surface arbitrary attributes.
    """
    if not entity_id:
        return {"error": "entity_id is required"}
    if _domain(entity_id) != "vacuum":
        return {"error": f"vacuum_control requires a vacuum entity_id, got {entity_id!r}"}

    ha = await _get_ha()
    service_map = {
        "start": "start",
        "pause": "pause",
        "stop": "stop",
        "return_to_base": "return_to_base",
        "clean_room": "start",  # the sim cycles all rooms; clean_room == start for now
    }
    service = service_map.get(action)
    if service is None:
        return {"error": f"unknown action {action!r}, expected one of {sorted(service_map)}"}
    if action == "clean_room" and not area:
        return {"error": "clean_room action requires `area` argument"}

    try:
        await ha.call_service(
            "vacuum", service, service_data={"entity_id": entity_id},
        )
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, entity_id)


@mcp.tool()
async def set_light(
    entity_id: str,
    brightness: Optional[int] = None,
    rgb_color: Optional[List[int]] = None,
    color_temp_kelvin: Optional[int] = None,
) -> Dict[str, Any]:
    """Adjust a light's brightness or color without toggling on/off.

    ``entity_id`` MUST be an exact light ID such as
    ``light.kitchen_counter``. If unsure, call ``list_devices`` first.

    If the light is off, this turns it on at the new setting. Pick
    this over ``turn_on`` when the user asks for a change in level
    or color, not a state change. At least one of ``brightness``,
    ``rgb_color``, ``color_temp_kelvin`` must be provided.
    """
    if not entity_id:
        return {"error": "entity_id is required"}
    if _domain(entity_id) != "light":
        return {"error": f"set_light requires a light entity_id, got {entity_id!r}"}
    if brightness is None and rgb_color is None and color_temp_kelvin is None:
        return {"error": "set_light requires at least one of brightness/rgb_color/color_temp_kelvin"}

    ha = await _get_ha()

    service_data: Dict[str, Any] = {"entity_id": entity_id}
    if brightness is not None:
        try:
            service_data["brightness"] = max(0, min(255, int(brightness)))
        except (TypeError, ValueError):
            return {"error": f"brightness must be int 0-255, got {brightness!r}"}
    if rgb_color is not None:
        if (
            not isinstance(rgb_color, (list, tuple))
            or len(rgb_color) != 3
            or not all(isinstance(c, (int, float)) for c in rgb_color)
        ):
            return {"error": f"rgb_color must be [R, G, B] ints, got {rgb_color!r}"}
        service_data["rgb_color"] = [max(0, min(255, int(c))) for c in rgb_color]
    if color_temp_kelvin is not None:
        try:
            service_data["color_temp_kelvin"] = int(color_temp_kelvin)
        except (TypeError, ValueError):
            return {
                "error": f"color_temp_kelvin must be int, got {color_temp_kelvin!r}"
            }

    # `light.turn_on` accepts the same payload whether the light is
    # on or off; HA handles the toggle implicitly when needed.
    try:
        await ha.call_service("light", "turn_on", service_data=service_data)
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, entity_id)


# --------------------------------------------------------------------------- #
# Scenes                                                                      #
# --------------------------------------------------------------------------- #
# HA-native scene services map cleanly onto four agent tools:
#
#   scene.turn_on  ->  apply_scene(scene_id)            apply a saved scene
#   scene.apply    ->  apply_states(entities)           one-shot, no save
#   scene.create   ->  save_scene(scene_id, ...)        register new scene
#   scene.delete   ->  delete_scene(scene_id)           remove a scene
#
# Curated scenes live in ha-config/scenes.yaml (persistent). Scenes
# created via save_scene at runtime are transient (HA loses them on
# restart) — same behaviour as HA's UI-driven scene-create flow.


# Filler words/suffixes the agent (and users) sprinkle into scene
# references. "my evening scene" / "the evening scene" / "evening
# scene" / "evening" should all collapse onto the same scene_id so
# save_scene + apply_scene + delete_scene stay consistent across
# turns even when the model paraphrases between them.
_SCENE_FILLER_PREFIXES = ("my ", "the ", "a ")
_SCENE_FILLER_SUFFIXES = (" scene", " mode", " preset")


def _scene_entity_id(scene_id: str) -> str:
    """Normalise an agent-supplied scene reference to a full entity_id.

    Examples (all collapse to ``scene.evening``):
        "evening", "Evening", "evening scene", "the evening scene",
        "my Evening Scene", "scene.evening"

    The full-entity-id input form is passed through unchanged
    (after lowercasing).
    """
    s = (scene_id or "").strip().lower()
    if not s:
        return ""
    if "." in s:
        # Already a full entity_id; just normalise case.
        return s
    # Slugify first so the strip patterns work whether the agent
    # passes "my evening scene", "my_evening_scene", or "My Evening".
    s = s.replace(" ", "_").replace("-", "_")
    s = "".join(c for c in s if c.isalnum() or c == "_").strip("_")
    # Then strip filler prefix/suffix tokens off the slug. Loop so
    # a chained "the_my_evening_scene" still collapses cleanly.
    changed = True
    while changed:
        changed = False
        for pre in _SCENE_FILLER_PREFIXES:
            tok = pre.strip().rstrip(" ") + "_"
            if s.startswith(tok):
                s = s[len(tok):]
                changed = True
                break
        for suf in _SCENE_FILLER_SUFFIXES:
            tok = "_" + suf.strip().lstrip(" ")
            if s.endswith(tok):
                s = s[: -len(tok)]
                changed = True
                break
    s = s.strip("_")
    return f"scene.{s}" if s else ""


@mcp.tool()
async def apply_scene(scene_id: str) -> Dict[str, Any]:
    """Apply a saved scene by name.

    Scenes are named multi-device states stored either in HA
    (curated scenes from scenes.yaml — always available) or in HA's
    runtime memory (created via ``save_scene`` during a session).

    Use this whenever the user references a scene by name: "movie
    night", "cozy bedroom", "morning kitchen", "goodnight", or any
    scene the user has saved earlier. ``scene_id`` accepts either
    a slug (``movie_night``) or the full entity_id
    (``scene.movie_night``); friendly forms like "Movie Night" are
    also slugified automatically.

    For one-shot multi-device control without saving a scene, use
    ``apply_states`` instead.

    Returns the scene's resulting state, or ``{"error": "..."}``.
    """
    eid = _scene_entity_id(scene_id)
    if not eid:
        return {"error": "scene_id is required"}
    if not eid.startswith("scene."):
        return {"error": f"resolved entity_id is not a scene: {eid!r}"}
    ha = await _get_ha()
    try:
        await ha.call_service(
            "scene", "turn_on", service_data={"entity_id": eid},
        )
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, eid)


@mcp.tool()
async def apply_states(entities: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Apply a one-shot collection of entity states atomically.

    Use this for "set the kitchen for cooking" / "make it cozy in
    here" style requests where the user wants a multi-device
    change but isn't asking to save the configuration as a named
    scene. Equivalent to HA's ``scene.apply`` service.

    ``entities`` is a dict mapping entity_id to a state payload:

        {
          "light.kitchen_counter": {"state": "on", "brightness": 240},
          "switch.coffee_machine": {"state": "on"},
          "cover.living_room_blind": {"state": "open", "current_position": 80}
        }

    All states apply together (HA orchestrates the parallel writes).
    No scene is registered — fire-and-forget. This is the fastest
    path for multi-device control: one tool call instead of N
    separate turn_on / set_light / set_cover_position calls.

    Returns ``{"applied": <count>, "entities": [...]}`` or
    ``{"error": "..."}``.
    """
    if not isinstance(entities, dict) or not entities:
        return {"error": "entities must be a non-empty object"}
    # Light validation: every value must be a dict.
    for eid, payload in entities.items():
        if not isinstance(payload, dict):
            return {"error": f"entity {eid!r} value must be an object, got {type(payload).__name__}"}

    ha = await _get_ha()
    try:
        await ha.call_service(
            "scene", "apply", service_data={"entities": entities},
        )
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return {"applied": len(entities), "entities": sorted(entities.keys())}


# Domains whose entities are meaningful to capture when the user
# says "save the current state" without specifying which devices.
# Sensors, binary_sensors, vacuum-attribute mirrors etc. are
# excluded — capturing read-only state in a scene doesn't help.
_SAVE_SCENE_SNAPSHOT_DOMAINS = frozenset({
    "light", "switch", "lock", "cover", "climate",
})


@mcp.tool()
async def save_scene(
    scene_id: str,
    entities: Optional[Dict[str, Dict[str, Any]]] = None,
    snapshot_entities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Save a new scene by name.

    Three calling modes:

    * **Snapshot the current state** (most common): call with just
      ``scene_id``. Captures the current state of every user-facing
      device (lights, switches, locks, blinds, thermostat) into the
      scene. Use this when the user says "save this as movie night"
      / "save the current setup as evening" / "remember this as my
      cooking scene" — the user means "whatever is on right now".

    * **Snapshot specific entities**: pass ``snapshot_entities``
      as a list of entity_ids. Same mechanism, but limited scope.
      Use when the user says "save just the bedroom lights as cozy".

    * **Explicit states**: pass ``entities`` as a dict of
      entity_id -> state payload. Use when describing a scene from
      scratch ("create a scene called Bright with all lights at 100%").

    Provide at most one of ``entities`` / ``snapshot_entities``.
    Omitting both triggers the default snapshot-everything mode.

    Scenes saved via this tool are transient: they're registered
    in HA's runtime and survive within the session but are lost on
    HA restart. Curated permanent scenes live in
    ``ha-config/scenes.yaml``.

    ``scene_id`` is slugified — "Movie Night" becomes "movie_night".

    Returns the resulting scene state.
    """
    eid = _scene_entity_id(scene_id)
    if not eid:
        return {"error": "scene_id is required"}
    slug = eid.split(".", 1)[1]
    if entities and snapshot_entities:
        return {"error": "provide only one of entities or snapshot_entities"}

    service_data: Dict[str, Any] = {"scene_id": slug}
    if entities:
        if not isinstance(entities, dict) or not entities:
            return {"error": "entities must be a non-empty object"}
        for k, v in entities.items():
            if not isinstance(v, dict):
                return {"error": f"entity {k!r} value must be an object"}
        service_data["entities"] = entities
    elif snapshot_entities:
        if not isinstance(snapshot_entities, list) or not snapshot_entities:
            return {"error": "snapshot_entities must be a non-empty list"}
        if not all(isinstance(s, str) for s in snapshot_entities):
            return {"error": "snapshot_entities must contain strings"}
        service_data["snapshot_entities"] = snapshot_entities
    else:
        # Default: snapshot every user-facing controllable device.
        # The agent rarely wants to enumerate these by hand, and
        # HA needs at least one entity in the snapshot list.
        ha = await _get_ha()
        states, _, _ = await _gather_registries(ha)
        snapshot = [
            s["entity_id"] for s in states
            if s.get("entity_id", "").split(".", 1)[0]
            in _SAVE_SCENE_SNAPSHOT_DOMAINS
        ]
        if not snapshot:
            return {"error": "no controllable devices found to snapshot"}
        service_data["snapshot_entities"] = snapshot

    ha = await _get_ha()
    try:
        await ha.call_service("scene", "create", service_data=service_data)
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, eid)


@mcp.tool()
async def delete_scene(scene_id: str) -> Dict[str, Any]:
    """Remove a saved scene by name.

    Works on transient scenes (those created via ``save_scene``)
    and on the curated scenes loaded from scenes.yaml. After
    deletion the scene is no longer available to ``apply_scene``.
    """
    eid = _scene_entity_id(scene_id)
    if not eid:
        return {"error": "scene_id is required"}
    if not eid.startswith("scene."):
        return {"error": f"resolved entity_id is not a scene: {eid!r}"}
    ha = await _get_ha()
    try:
        await ha.call_service(
            "scene", "delete", service_data={"entity_id": eid},
        )
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return {"deleted": eid}


# --------------------------------------------------------------------------- #
# Event buffer access                                                         #
# --------------------------------------------------------------------------- #


@mcp.tool()
async def list_recent_events(
    limit: int = 10,
    since: Optional[str] = None,
    kinds: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """List recent significant home events.

    Use this tool when the user asks "what happened", "any activity",
    "anything new", "did the door open", etc. The buffer holds the
    most recent transitions on doors, windows, the leak sensor, the
    smoke detector, locks, and the vacuum (motion sensors are
    intentionally excluded — they fire too often to be useful as
    alerts).

    Returns up to ``limit`` events newest-first. Filters:
    ``since`` (ISO 8601 timestamp; only events after that time) and
    ``kinds`` (list of: contact_open, contact_close, leak, smoke,
    lock_changed, vacuum_state).

    Each event has: kind, entity_id, friendly_name, area, state,
    previous_state, timestamp (ISO 8601 UTC).
    """
    # Lazy-subscribe: if HA WS dropped at some point and reconnected,
    # we may not be subscribed any more. Cheap no-op when already
    # active.
    try:
        await _ensure_events_subscribed()
    except Exception as exc:
        return {"error": f"could not subscribe to HA events: {exc}"}

    try:
        limit_v = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        return {"error": f"limit must be an integer 1-50, got {limit!r}"}
    valid_kinds = {
        "contact_open", "contact_close", "leak", "smoke",
        "lock_changed", "vacuum_state",
    }
    if kinds is not None:
        if not isinstance(kinds, list):
            return {"error": f"kinds must be a list of strings, got {type(kinds).__name__}"}
        bad = [k for k in kinds if k not in valid_kinds]
        if bad:
            return {"error": f"unknown kind(s) {bad!r}; valid: {sorted(valid_kinds)}"}

    events = await _events.list(limit=limit_v, since=since, kinds=kinds)

    # Resolve area lazily here (we can do WS calls safely from a tool;
    # the reader-loop deadlock only applies to the event handler).
    if events:
        try:
            ha = await _get_ha()
            _, entities_by_id, devices_by_id = await _gather_registries(ha)
            for ev in events:
                if ev.get("area") is None:
                    entry = entities_by_id.get(ev.get("entity_id", ""))
                    ev["area"] = _resolve_area(entry, devices_by_id)
        except Exception:
            pass  # area resolution is best-effort
    return {"events": events, "count": len(events)}


# --------------------------------------------------------------------------- #
# SSE events endpoint                                                         #
# --------------------------------------------------------------------------- #
# Sits next to /mcp on the same FastMCP server. Consumers (home_agent)
# subscribe and receive every significant event in real time. The
# protocol is plain SSE, not MCP — MCP's notification-broadcast
# plumbing is awkward to fan out across sessions, and this is a pure
# server-push channel anyway. Events are JSON, one per "data:" line.


@mcp.custom_route("/events", methods=["GET"])
async def events_sse(_request: Request) -> StreamingResponse:
    """Server-Sent Events stream of significant home events.

    Each frame is a single SSE event of type ``home_event`` carrying
    the same payload shape as ``list_recent_events`` returns. We
    ensure the HA subscription is engaged before opening the stream
    so consumers don't miss events that fire during the connection
    handshake.
    """
    try:
        await _ensure_events_subscribed()
    except Exception as exc:
        log.warning("events SSE: HA subscribe failed (%s); streaming anyway", exc)

    queue = _events.subscribe()
    log.info("SSE /events: client connected (subscribers=%d)", len(_events._subscribers))

    async def stream() -> AsyncIterator[bytes]:
        try:
            # Send a hello so the client knows the stream is alive
            # even before any home event fires.
            yield b"event: hello\ndata: {}\n\n"
            while True:
                # Race the next event against a 20s heartbeat so
                # intermediate proxies / clients don't time out an
                # idle stream.
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    payload = json.dumps(event).encode("utf-8")
                    yield b"event: home_event\ndata: " + payload + b"\n\n"
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("SSE /events stream error")
        finally:
            _events.unsubscribe(queue)
            log.info("SSE /events: client disconnected (subscribers=%d)", len(_events._subscribers))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            "Connection": "keep-alive",
        },
    )


def main() -> None:
    log.info("Starting smart-home MCP server on %s:%s/mcp/", MCP_HOST, MCP_PORT)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
