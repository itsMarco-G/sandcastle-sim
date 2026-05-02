"""Simulator entry point.

Runs one MQTT client connection, instantiates every device from the
topology, publishes their HA discovery payloads, subscribes to the
combined set of command topics, and dispatches inbound commands to
the right device.

Behaviors (motion firing, temperature drift, vacuum movement, power
meter) land in milestone 7. For milestone 4 the simulator just needs
to register every entity and let the controllable ones (lights,
switches, locks, covers, climate, media_player, vacuum) react to
commands.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Dict, List

import aiomqtt
from dotenv import load_dotenv

from .base import Device
from .behaviors import behavior_tasks
from .climate import Climate
from .control import DEFAULT_HOST, DEFAULT_PORT, run_control_server
from .covers import Cover
from .lights import Light
from .locks import Lock
from .sensors import BinarySensor, Sensor
from .switches import Switch
from .topology import (
    BINARY_SENSORS,
    CLIMATES,
    COVERS,
    LIGHTS,
    LOCKS,
    SENSORS,
    SWITCHES,
    VACUUMS,
    total_devices,
)
from .vacuum import Vacuum

# Load .env so HA_URL / HA_TOKEN are available to the control server,
# and so the user can override MQTT_HOST etc. without setting them on
# the shell. find_dotenv walks up from cwd, so this works whether
# the user installed sandcastle-sim editable from the repo or via pip.
load_dotenv(override=False)

log = logging.getLogger(__name__)

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))


def _build_devices(mqtt: aiomqtt.Client) -> List[Device]:
    """Instantiate every device from the topology."""
    devices: List[Device] = []
    for spec in LIGHTS:
        devices.append(Light(mqtt, spec))
    for spec in SWITCHES:
        devices.append(Switch(mqtt, spec))
    for spec in LOCKS:
        devices.append(Lock(mqtt, spec))
    for spec in COVERS:
        devices.append(Cover(mqtt, spec))
    for spec in CLIMATES:
        devices.append(Climate(mqtt, spec))
    for spec in SENSORS:
        devices.append(Sensor(mqtt, spec))
    for spec in BINARY_SENSORS:
        devices.append(BinarySensor(mqtt, spec))
    # MEDIA_PLAYERS is intentionally empty (deferred — see contract).
    for spec in VACUUMS:
        devices.append(Vacuum(mqtt, spec))
    return devices


def _command_subscriptions(devices: List[Device]) -> Dict[str, Device]:
    """Map each command topic the simulator listens on to its device.

    Most domains use a single ``command_topic``; climate uses two
    (mode + temperature). Covers route position-set through their
    main ``command_topic`` so we don't need to track that separately.
    """
    routes: Dict[str, Device] = {}
    for d in devices:
        # Climate has multiple command topics; everything else has
        # one (or none).
        if isinstance(d, Climate):
            for topic in d.command_topics():
                routes[topic] = d
        elif d.has_command_topic():
            routes[d.command_topic] = d
    return routes


async def _mqtt_dispatch_loop(
    mqtt: aiomqtt.Client,
    routes: Dict[str, Device],
    stop_event: asyncio.Event,
) -> None:
    """Forward inbound MQTT messages to the owning device.

    Stops cleanly when ``stop_event`` is set — needed so the
    surrounding ``asyncio.gather`` can shut down both the MQTT side
    and the HTTP control server on Ctrl-C.
    """
    iterator = aiter(mqtt.messages)
    while not stop_event.is_set():
        # Race the next MQTT message against stop_event so a Ctrl-C
        # during an idle period still tears the loop down promptly.
        next_task = asyncio.create_task(anext(iterator))
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {next_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if stop_task in done:
            return
        try:
            msg = next_task.result()
        except StopAsyncIteration:
            return
        topic = str(msg.topic)
        target = routes.get(topic)
        if target is None:
            log.debug("no device for command topic %s; dropping", topic)
            continue
        try:
            if isinstance(target, Climate):
                await target.handle_command(msg.payload, topic=topic)
            else:
                await target.handle_command(msg.payload)
        except Exception:
            log.exception(
                "device %s raised on command %r",
                target.unique_id, msg.payload[:120],
            )


async def _run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info(
        "Smart Home simulator starting — connecting to MQTT %s:%s",
        MQTT_HOST, MQTT_PORT,
    )

    async with aiomqtt.Client(MQTT_HOST, MQTT_PORT) as mqtt:
        devices = _build_devices(mqtt)
        log.info(
            "Built %d devices (expected %d from topology)",
            len(devices), total_devices(),
        )

        # Subscribe BEFORE publishing discovery so the broker doesn't
        # forward retained command-topic messages to us out of order.
        # (HA never publishes retained commands, but anyone debugging
        # by hand might.)
        routes = _command_subscriptions(devices)
        for topic in routes:
            await mqtt.subscribe(topic)
        log.info("Subscribed to %d command topics", len(routes))

        # Publish discovery for every device, then initial state.
        for d in devices:
            await d.publish_discovery()
        # Small grace period — HA needs ~50–100 ms per discovery
        # message to set up the entity. Sleeping briefly here keeps
        # the initial state we publish from arriving before the
        # entity exists, which would otherwise be a missed update.
        await asyncio.sleep(0.5)
        for d in devices:
            await d.publish_state()
        log.info(
            "Published discovery + initial state for all %d devices",
            len(devices),
        )

        # Run MQTT dispatch + HTTP control server + behaviour tasks
        # concurrently. The control server hosts the floor-plan GUI
        # and the demo-trigger API for sensor events; the behaviours
        # power motion firing, temperature drift, vacuum movement,
        # and the power meter.
        stop_event = asyncio.Event()
        bg_tasks = behavior_tasks(devices, stop_event)
        try:
            await asyncio.gather(
                _mqtt_dispatch_loop(mqtt, routes, stop_event),
                run_control_server(
                    devices,
                    host=DEFAULT_HOST,
                    port=DEFAULT_PORT,
                    stop_event=stop_event,
                ),
                *bg_tasks,
            )
        except asyncio.CancelledError:
            stop_event.set()
            for t in bg_tasks:
                t.cancel()
            raise


def main() -> None:
    # Cooperate with Ctrl-C cleanly. aiomqtt's context manager
    # publishes the offline LWT and tears down the connection on
    # KeyboardInterrupt — letting the exception propagate is the
    # cleanest path.
    def _sigterm_handler(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Simulator shutting down on signal")


if __name__ == "__main__":
    main()
