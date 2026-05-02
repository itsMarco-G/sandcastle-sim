"""Background behaviours that make the simulated home feel alive.

Each behaviour is a long-running coroutine the simulator gathers
alongside MQTT dispatch and the HTTP control server. They read
device state directly from the device objects (the simulator owns
the source of truth) and drive updates by calling each device's
publish_state.

Behaviours implemented:

* Motion sensors fire short ON pulses on a per-sensor schedule.
  Hallway sensor fires more often than living-room (matches typical
  household traffic).
* Bedroom temperature sensors drift via a gentle random walk plus a
  pull toward the thermostat's target when in heat / cool mode.
* Robot vacuum cycles through rooms while cleaning; ``current_room``
  is mirrored to ``sensor.vacuum_current_room`` so HA + GUI can read
  it (HA's mqtt.vacuum integration drops non-standard attributes).
* Power meter sums wattage of all active devices on a 2 s cadence:
  fridge baseline, on-lights, coffee machine, climate when active,
  vacuum when cleaning.

Determinism: behaviours seed Python's random module from a fixed
default so motion fires in roughly the same rhythm across runs —
demos read better when the timing isn't wildly different each time.
Override with ``BEHAVIOR_SEED`` env var.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Dict, List, Optional

from .climate import Climate
from .lights import Light
from .sensors import BinarySensor, Sensor
from .switches import Switch
from .vacuum import Vacuum

log = logging.getLogger(__name__)

# Deterministic-ish randomness for demos.
_SEED = int(os.environ.get("BEHAVIOR_SEED", "1337"))
_rng = random.Random(_SEED)


# --------------------------------------------------------------------- #
# Motion sensor pulses                                                  #
# --------------------------------------------------------------------- #
# Each motion sensor fires short ON pulses with random idle gaps. The
# distributions are chosen so the demo feels lively but doesn't spam
# the event panel — about one motion event per minute per sensor on
# average.

# (slug -> (idle_min_s, idle_max_s, pulse_min_s, pulse_max_s))
_MOTION_SCHEDULES = {
    "hallway_motion":     (40, 120, 3, 6),
    "living_room_motion": (60, 180, 3, 8),
}


async def motion_pulse_loop(sensor: BinarySensor, stop_event: asyncio.Event) -> None:
    schedule = _MOTION_SCHEDULES.get(sensor.slug)
    if schedule is None:
        return
    idle_min, idle_max, pulse_min, pulse_max = schedule
    # Stagger initial pulses so they don't all land together at boot.
    await asyncio.sleep(_rng.uniform(5, idle_min))
    while not stop_event.is_set():
        try:
            await sensor.set(True)
            await asyncio.sleep(_rng.uniform(pulse_min, pulse_max))
            await sensor.set(False)
            await asyncio.sleep(_rng.uniform(idle_min, idle_max))
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("motion behaviour for %s failed", sensor.unique_id)
            await asyncio.sleep(5)


# --------------------------------------------------------------------- #
# Temperature drift                                                     #
# --------------------------------------------------------------------- #
# Bedroom temperature sensors AND the thermostat's own current_temperature
# wander by a small random walk every tick. When the thermostat has a
# target and a non-off mode, every temperature point pulls toward the
# target — including in `auto`, which is the default and what users
# expect to "just work" when they say "set the temperature to 22".
#
# Tick interval and pull rate were originally tuned for "feels real
# over hours" but that meant a demo prompt produced no visible motion
# for minutes. Cranked toward "demo-readable in seconds" instead.

_TEMP_INTERVAL_S = 2.0   # was 8.0 — needed sub-minute visible motion
_TEMP_STEP = 0.05        # random walk amplitude
_TEMP_PULL = 0.25        # was 0.04 — strong pull so 5°C target lands in ~30s


def _drift_delta(current: float, target: Optional[float], mode: Optional[str]) -> float:
    """Return the per-tick temperature delta for one point.

    Random walk plus a directional pull when the thermostat is
    actively trying to reach a setpoint. ``auto`` mode pulls in
    either direction toward the target (the behavior users expect
    from the default mode). ``heat`` / ``cool`` only pull in the
    direction matching their action. ``off`` is pure random walk.
    """
    step = _rng.uniform(-_TEMP_STEP, _TEMP_STEP)
    if target is None or mode in (None, "off"):
        return step
    diff = target - current
    if mode == "heat" and diff > 0:
        return step + _TEMP_PULL
    if mode == "cool" and diff < 0:
        return step - _TEMP_PULL
    if mode == "auto" and abs(diff) > 0.1:
        return step + (_TEMP_PULL if diff > 0 else -_TEMP_PULL)
    return step


async def temperature_drift_loop(
    temp_sensors: List[Sensor],
    thermostat: Optional[Climate],
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.sleep(_TEMP_INTERVAL_S)
            target = None
            mode = None
            if thermostat is not None:
                target = thermostat.state.get("target_temperature")
                mode = thermostat.state.get("hvac_mode")

            # Bedroom sensors.
            for sensor in temp_sensors:
                cur = sensor.value
                new_val = round(cur + _drift_delta(cur, target, mode), 1)
                new_val = max(15.0, min(28.0, new_val))
                if abs(new_val - cur) >= 0.05:
                    await sensor.set_value(new_val)

            # The thermostat's own current_temperature — was being
            # left static by the loop, which is why "set temperature
            # to 25" didn't visibly do anything: the target updated
            # but the current temp it's compared against in the GUI
            # never moved.
            if thermostat is not None:
                cur = float(thermostat.state.get("current_temperature", 20.0))
                new_val = round(cur + _drift_delta(cur, target, mode), 1)
                new_val = max(15.0, min(28.0, new_val))
                if abs(new_val - cur) >= 0.05:
                    thermostat.state["current_temperature"] = new_val
                    await thermostat.publish_state()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("temperature drift behaviour failed")


# --------------------------------------------------------------------- #
# Vacuum room cycling                                                   #
# --------------------------------------------------------------------- #
# When the vacuum is cleaning, advance its current_room every ~12 s
# through a sensible cycle: living_room -> kitchen -> hallway ->
# bedroom -> bedroom_2 -> bathroom -> back to living_room.
# When returning, reaches docked after ~6 s.

_VACUUM_CYCLE = ["living_room", "kitchen", "hallway", "bedroom", "bedroom_2", "bathroom"]
_VACUUM_DWELL_S = 12.0
_VACUUM_RETURN_S = 6.0


async def vacuum_movement_loop(
    vacuum: Vacuum,
    room_sensor: Optional[Sensor],
    stop_event: asyncio.Event,
) -> None:
    cycle_idx = 0
    returning_since: Optional[float] = None
    last_state = vacuum.state.get("state")
    last_change = asyncio.get_event_loop().time()

    async def _set_room(room_key: Optional[str]) -> None:
        vacuum.state["current_room"] = room_key
        await vacuum.publish_state()
        if room_sensor is not None:
            label = room_key if room_key else (vacuum.state.get("state") or "docked")
            await room_sensor.set_value(label)

    await _set_room(None)

    while not stop_event.is_set():
        try:
            await asyncio.sleep(1.0)
            now = asyncio.get_event_loop().time()
            cur_state = vacuum.state.get("state")

            # Reset timers on state transitions.
            if cur_state != last_state:
                last_state = cur_state
                last_change = now
                returning_since = now if cur_state == "returning" else None
                cycle_idx = 0
                if cur_state == "cleaning":
                    await _set_room(_VACUUM_CYCLE[0])
                elif cur_state in ("docked", "idle"):
                    await _set_room(None)

            if cur_state == "cleaning" and (now - last_change) >= _VACUUM_DWELL_S:
                cycle_idx = (cycle_idx + 1) % len(_VACUUM_CYCLE)
                last_change = now
                await _set_room(_VACUUM_CYCLE[cycle_idx])
            elif cur_state == "returning":
                if returning_since is None:
                    returning_since = now
                if (now - returning_since) >= _VACUUM_RETURN_S:
                    vacuum.state["state"] = "docked"
                    await vacuum.publish_state()
                    await _set_room(None)
                    returning_since = None
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("vacuum movement behaviour failed")
            await asyncio.sleep(5)


# --------------------------------------------------------------------- #
# Power meter                                                           #
# --------------------------------------------------------------------- #
# Sum a simplified wattage model and publish to sensor.power_meter
# every 2 s. The model is deliberately approximate — what matters
# for the demo is that turning on the coffee machine kicks the
# meter up, and dimming a light lowers it.

_POWER_TICK_S = 2.0
_FRIDGE_BASELINE_W = 90.0
_VACUUM_W = 35.0
_HEAT_W = 1500.0
_COOL_W = 1200.0


async def power_meter_loop(
    lights: List[Light],
    switches: List[Switch],
    climate_unit: Optional[Climate],
    vacuum: Optional[Vacuum],
    power_sensor: Optional[Sensor],
    temp_sensors: List[Sensor],
    stop_event: asyncio.Event,
) -> None:
    if power_sensor is None:
        return
    while not stop_event.is_set():
        try:
            await asyncio.sleep(_POWER_TICK_S)
            total = _FRIDGE_BASELINE_W

            for light in lights:
                if light.state.get("state") == "ON":
                    # 12 W when at full brightness, scaled linearly.
                    b = light.state.get("brightness", 200) or 0
                    total += 12.0 * (b / 255.0)

            for sw in switches:
                if sw.is_on:
                    total += sw.watts_when_on

            if climate_unit is not None:
                mode = climate_unit.state.get("hvac_mode")
                tgt = climate_unit.state.get("target_temperature")
                avg_temp = (
                    sum(s.value for s in temp_sensors) / max(1, len(temp_sensors))
                    if temp_sensors else 21.0
                )
                if mode == "heat" and tgt is not None and avg_temp < tgt - 0.1:
                    total += _HEAT_W
                elif mode == "cool" and tgt is not None and avg_temp > tgt + 0.1:
                    total += _COOL_W

            if vacuum is not None and vacuum.state.get("state") == "cleaning":
                total += _VACUUM_W

            await power_sensor.set_value(round(total, 0))
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("power meter behaviour failed")


# --------------------------------------------------------------------- #
# Convenience: launch everything                                        #
# --------------------------------------------------------------------- #


def behavior_tasks(devices, stop_event: asyncio.Event) -> List[asyncio.Task]:
    """Return a list of background tasks for every behaviour we run.

    Caller is responsible for awaiting / cancelling them. The simulator
    main loop folds these into its asyncio.gather.
    """
    by_id: Dict[str, object] = {f"{d.domain}.{d.slug}": d for d in devices}

    motion_sensors = [
        d for d in devices
        if isinstance(d, BinarySensor)
        and d.device_class == "motion"
        and d.slug in _MOTION_SCHEDULES
    ]
    temp_sensors = [
        d for d in devices
        if isinstance(d, Sensor) and d.device_class == "temperature"
    ]
    lights = [d for d in devices if isinstance(d, Light)]
    switches = [d for d in devices if isinstance(d, Switch)]
    climate_units = [d for d in devices if isinstance(d, Climate)]
    vacuums = [d for d in devices if isinstance(d, Vacuum)]
    power_sensor = next(
        (d for d in devices if isinstance(d, Sensor) and d.slug == "power_meter"),
        None,
    )
    room_sensor = next(
        (d for d in devices if isinstance(d, Sensor) and d.slug == "vacuum_current_room"),
        None,
    )

    tasks: List[asyncio.Task] = []
    for s in motion_sensors:
        tasks.append(asyncio.create_task(
            motion_pulse_loop(s, stop_event), name=f"motion-{s.slug}",
        ))
    if temp_sensors:
        tasks.append(asyncio.create_task(
            temperature_drift_loop(
                temp_sensors,
                climate_units[0] if climate_units else None,
                stop_event,
            ),
            name="temp-drift",
        ))
    for v in vacuums:
        tasks.append(asyncio.create_task(
            vacuum_movement_loop(v, room_sensor, stop_event),
            name=f"vacuum-{v.slug}",
        ))
    tasks.append(asyncio.create_task(
        power_meter_loop(
            lights, switches,
            climate_units[0] if climate_units else None,
            vacuums[0] if vacuums else None,
            power_sensor, temp_sensors,
            stop_event,
        ),
        name="power-meter",
    ))
    log.info("Started %d background behaviour task(s)", len(tasks))
    return tasks
