# Smart Home — MCP Tool Contract (v0.2)

This is the **canonical contract** for the smart-home tool surface.
The MCP server in `src/sandcastle_sim/mcp_server/` implements it; the agent
consumes it via MCP `tools/list` + `tools/call` over streamable HTTP.

If a tool name, parameter, or return field changes, **this document
changes first** and the version above ticks up.

---

## 1. Conventions

### Entity ID format

`{domain}.{area}_{slug}` — lowercase, snake_case, no spaces.

Examples — the actual entity_ids the v0.1 simulator publishes:

- `light.living_room_main`, `light.living_room_accent`,
  `light.kitchen_counter`, `light.hallway_ceiling`,
  `light.bedroom_mood`, `light.bedroom_2_main`
- `switch.coffee_machine`
- `lock.front_door`
- `cover.living_room_blind`, `cover.bedroom_blind`
- `climate.home_thermostat`
- `sensor.bedroom_temperature`, `sensor.bedroom_2_temperature`,
  `sensor.power_meter`
- `binary_sensor.front_door_contact`,
  `binary_sensor.kitchen_window_contact`,
  `binary_sensor.hallway_motion`, `binary_sensor.living_room_motion`,
  `binary_sensor.kitchen_leak`, `binary_sensor.hallway_smoke`
- `vacuum.robot_vacuum`
- (no `media_player` entities — speaker deferred)

These match Home Assistant's entity-ID convention exactly. The MCP
server does not invent an ID format; it surfaces what HA has. The
slugs come from `slugify(device.name)` per HA's MQTT-discovery
behaviour, which is why some include words like `_thermostat` /
`_vacuum` / `_main` rather than the bare-room form.

### Domains used

| Domain          | Used for                                          |
| --------------- | ------------------------------------------------- |
| `light`         | All lights (dimmable + RGB)                       |
| `switch`        | Smart plug (coffee machine)                       |
| `lock`          | Door lock                                         |
| `cover`         | Blinds                                            |
| `climate`       | Thermostat                                        |
| `sensor`        | Numeric sensors (temperature, power)              |
| `binary_sensor` | On/off sensors (motion, contact, leak, smoke)     |
| `media_player`  | Speaker (deferred — see `media_control` below)    |
| `vacuum`        | Robot vacuum                                      |

### Areas

| Area key      | Display name |
| ------------- | ------------ |
| `living_room` | Living Room  |
| `kitchen`     | Kitchen      |
| `hallway`     | Hallway      |
| `bedroom`     | Bedroom      |
| `bedroom_2`   | Bedroom 2    |
| `bathroom`    | Bathroom     |

`climate.home_thermostat` and `vacuum.robot_vacuum` are whole-home,
not bound to one area; their `area` field is `null`.

### Friendly-name resolution

**The agent resolves friendly names; the MCP server does not.**

`list_devices` returns rich metadata (`entity_id`, `friendly_name`,
`area`, `domain`, `state`, `attributes`). The agent uses that
metadata to map "the kitchen light" to `light.kitchen_main` before
calling a control tool. Control tools accept entity IDs only.

This keeps the tool surface simple, predictable, and faithful to how
HA's own services work. If the agent gets it wrong, that's a model
problem to solve in the system prompt, not a tool-contract problem.

### Error convention

Every tool returns a JSON object. On failure, the object includes a
top-level `"error"` field with a human-readable string and **no
exception is raised**. Examples:

```json
{"error": "Unknown entity_id: light.does_not_exist"}
{"error": "Cannot turn_on a lock; use lock instead"}
{"error": "HA WebSocket unavailable: Connection refused"}
```

This matches the `home_agent_perf` error convention — the agent's
loop surfaces `error` keys to the LLM, which can retry.

### Result shape principles

- **Flat**, not deeply nested. The model handles flat objects best.
- **Human-meaningful keys** (`brightness`, `temperature`), not
  internal names.
- **Numbers as numbers**, not strings.
- **`null` or omit** when something doesn't apply (don't invent zeros).
- After every control tool, return the **resulting state** so the
  agent can self-confirm.

---

## 2. The tool surface

Twelve tools. Three categories: discovery, control, events.

### Discovery

#### `list_areas`

Return every area defined in HA.

**Parameters**: none.

**Returns**:
```json
{
  "areas": [
    {"key": "living_room", "name": "Living Room"},
    {"key": "kitchen",     "name": "Kitchen"},
    {"key": "hallway",     "name": "Hallway"},
    {"key": "bedroom",     "name": "Bedroom"},
    {"key": "bedroom_2",   "name": "Bedroom 2"},
    {"key": "bathroom",    "name": "Bathroom"}
  ]
}
```

**Description (for the LLM)**:
> "Return every area (room) defined in the home. Call this when the
> user asks 'what rooms do I have' or you need to disambiguate
> which room a device is in. Cheap; safe to call eagerly."

---

#### `list_devices`

Return every controllable / observable device.

**Parameters**:
- `area` (string, optional): filter to one area key. Omit to get
  all devices.
- `domain` (string, optional): filter to one domain (e.g.
  `"light"`). Omit to get all domains.

**Returns**:
```json
{
  "devices": [
    {
      "entity_id": "light.kitchen_main",
      "friendly_name": "Kitchen Main Light",
      "domain": "light",
      "area": "kitchen",
      "state": "on",
      "attributes": {
        "brightness": 200,
        "supported_color_modes": ["brightness"],
        "color_mode": "brightness"
      },
      "protocol": "mqtt"
    }
  ],
  "count": 17
}
```

The `protocol` field is `"mqtt"` for everything in the demo today;
it'll be `"matter"` for any device commissioned via the optional
Matter integration. The GUI uses it for a small badge.

**Description (for the LLM)**:
> "List the devices in the home. Use this whenever you need to know
> what exists, what state it's in, or which entity_id corresponds
> to a friendly name like 'kitchen light'. Filter by `area` or
> `domain` to keep results focused. Always call this before a
> control tool if you're unsure of the exact entity_id."

---

#### `get_device_state`

Return the current state of a single entity.

**Parameters**:
- `entity_id` (string, required)

**Returns**: same shape as one element of `list_devices.devices`.

**Description (for the LLM)**:
> "Read the current state of a single device. Useful after a
> control action to confirm it took effect, or when the user asks
> about one specific thing. For multi-device queries, use
> `list_devices`."

---

### Control

All control tools return the entity's resulting state on success
(same shape as `get_device_state`'s return), or `{"error": "..."}`
on failure.

---

#### `turn_on`

Turn on any toggleable entity (lights, switches). Lights respect
optional `brightness` and `rgb_color`.

**Parameters**:
- `entity_id` (string, required)
- `brightness` (integer 0–255, optional): light only
- `rgb_color` (array of 3 integers 0–255, optional): RGB lights only

**Returns**: resulting `{entity_id, state, attributes, ...}` or error.

**Description (for the LLM)**:
> "Turn on a light or switch. For lights, you can optionally set
> `brightness` (0–255) and `rgb_color` ([R, G, B], 0–255 each, RGB
> lights only). Don't use this for locks (use `lock` / `unlock`),
> blinds (use `set_cover_position`), or the thermostat (use
> `set_climate`)."

---

#### `turn_off`

Turn off any toggleable entity (lights, switches).

**Parameters**:
- `entity_id` (string, required)

**Returns**: resulting state or error.

**Description (for the LLM)**:
> "Turn off a light or switch. Same domain rules as `turn_on`."

---

#### `set_light`

Adjust a light without changing its on/off state if already on.
This is the right call for "dim the lights to 50%" or "make the
bedroom lamp warmer" — it doesn't toggle.

**Parameters**:
- `entity_id` (string, required)
- `brightness` (integer 0–255, optional)
- `rgb_color` (array of 3 integers 0–255, optional)
- `color_temp_kelvin` (integer, optional): color-temperature lights

At least one of `brightness` / `rgb_color` / `color_temp_kelvin`
must be present.

**Returns**: resulting state or error.

**Description (for the LLM)**:
> "Adjust a light's brightness or color without toggling on/off. If
> the light is off, this turns it on at the new setting. Pick this
> over `turn_on` when the user asks for a change in level or color,
> not a state change."

---

#### `lock`

Lock a lock entity.

**Parameters**:
- `entity_id` (string, required)

**Returns**: resulting state or error.

**Description (for the LLM)**:
> "Lock a smart lock. Returns the resulting state (`locked` /
> `unlocked` / `locking` / `jammed`)."

---

#### `unlock`

Unlock a lock entity.

**Parameters**:
- `entity_id` (string, required)

**Returns**: resulting state or error.

**Description (for the LLM)**:
> "Unlock a smart lock. Confirm with the user first if the request
> is ambiguous — unlocking a door is a security-sensitive action."

---

#### `set_cover_position`

Move a blind / cover to a position.

**Parameters**:
- `entity_id` (string, required)
- `position` (integer 0–100, required): 0 = fully closed,
  100 = fully open

**Returns**: resulting state or error.

**Description (for the LLM)**:
> "Move a blind to a position from 0 (closed) to 100 (open).
> Animation takes a few seconds; the returned state may be
> 'opening' / 'closing' until the simulator finishes the move."

---

#### `set_climate`

Adjust the thermostat.

**Parameters**:
- `entity_id` (string, required) — `climate.home_thermostat` for the demo
- `temperature` (number, optional): target temperature in °C
- `hvac_mode` (string, optional): one of `"heat"`, `"cool"`,
  `"auto"`, `"off"`

At least one of `temperature` / `hvac_mode` must be present.

**Returns**: resulting state or error.

**Description (for the LLM)**:
> "Set the thermostat's target temperature (in °C) or HVAC mode.
> The whole-home thermostat is `climate.home_thermostat`. Mode options:
> heat / cool / auto / off."

---

#### `media_control` (deferred to v0.2)

The speaker is **not implemented** in the v0.1 demo.

HA's MQTT integration does not support the `media_player` domain
natively, and modelling a speaker as `switch` + `number` would
introduce friction the contract is supposed to hide. We deliberately
omit it for now rather than ship a half-fit. When we revisit, the
options are:

- HA template `media_player` configured via yaml
- A custom HA integration shipped alongside this repo
- A composed pseudo-tool that calls `switch` + `number` underneath

The tool name `media_control` is reserved.

---

#### `vacuum_control`

Control the robot vacuum.

**Parameters**:
- `entity_id` (string, required) — `vacuum.robot_vacuum`
- `action` (string, required): one of `"start"`, `"pause"`,
  `"stop"`, `"return_to_base"`, `"clean_room"`
- `area` (string, optional): area key, required when
  `action == "clean_room"`

**Returns**: resulting state or error. The vacuum's
`current_room` attribute is part of the returned state and
updates as the simulator moves it between rooms.

**Description (for the LLM)**:
> "Control the robot vacuum. Actions: start, pause, stop,
> return_to_base, clean_room (also requires `area`, e.g.
> 'kitchen'). The vacuum reports `current_room` in its
> attributes."

---

### Scenes (v0.2)

Scenes are named multi-device states. Two flavours coexist:

- **Curated scenes** — defined in `ha-config/scenes.yaml`, persistent
  across HA restarts, available out-of-box. Demo seeds:
  `scene.movie_night`, `scene.cozy_bedroom`, `scene.morning_kitchen`,
  `scene.goodnight`.
- **Transient scenes** — created at runtime via `save_scene`. Live
  in HA's memory only; lost when HA restarts.

Both kinds are addressable by the same four tools below. Discovery
goes through `list_devices` (the `scene` domain is included).

The MCP server normalises scene_id liberally — agents can pass
"movie_night", "Movie Night", "the movie night scene", or
"scene.movie_night" and they all collapse to `scene.movie_night`.
Filler tokens (`my_`, `the_`, trailing `_scene` / `_mode` / `_preset`)
are stripped server-side so the slug stays consistent across turns
even when the model paraphrases.

---

#### `apply_scene`

Apply a saved scene by name.

**Parameters**:
- `scene_id` (string, required): slug or full entity_id

**Returns**: the resulting scene state (same shape as
`get_device_state`'s return), or `{"error": "..."}`.

**Description (for the LLM)**:
> "Apply a saved scene by name. Use whenever the user references
> a scene: 'movie night', 'cozy bedroom', 'goodnight', or any
> scene the user has saved earlier. For one-shot multi-device
> control without saving, use `apply_states` instead."

---

#### `apply_states`

Apply a one-shot multi-device state collection without saving.

**Parameters**:
- `entities` (object, required): mapping of entity_id → state payload.

**Example**:
```json
{
  "entities": {
    "light.kitchen_counter": {"state": "on", "brightness": 240},
    "switch.coffee_machine": {"state": "on"},
    "cover.living_room_blind": {"state": "open", "current_position": 80}
  }
}
```

**Returns**:
```json
{"applied": 3, "entities": ["light.kitchen_counter", ...]}
```

**Description (for the LLM)**:
> "Apply a one-shot collection of entity states atomically. Use for
> 'set the kitchen for cooking' / 'make it cozy in here' style
> requests where the user wants a multi-device change but isn't
> asking to save the configuration as a named scene. Equivalent to
> HA's `scene.apply` service — no scene is registered."

This is the **fastest path for multi-device control**: one tool
call instead of N separate `turn_on` / `set_light` / `set_cover_position`
calls. HA orchestrates the writes in parallel.

---

#### `save_scene`

Save a new scene by name. Three calling modes:

**Parameters**:
- `scene_id` (string, required): user-friendly name; slugified
- `entities` (object, optional): explicit dict of entity_id → state
- `snapshot_entities` (array of strings, optional): list of entity_ids
  whose CURRENT state to capture

Provide at most one of `entities` / `snapshot_entities`.
**Omitting both** triggers the default "snapshot all controllable
devices" mode (lights, switches, locks, covers, climate). This is
the most common case — "save this as movie night" with no other
arguments needed.

**Returns**: the resulting scene state, or `{"error": "..."}`.

**Description (for the LLM)**:
> "Save a new scene by name. Most common: just pass `scene_id`
> alone — the server snapshots all currently-controllable devices.
> Use `entities` to specify exact states from scratch; use
> `snapshot_entities` to limit which devices to capture.
> Saved scenes are transient (lost on HA restart). The agent
> can save and re-apply within a session."

---

#### `delete_scene`

Remove a scene.

**Parameters**:
- `scene_id` (string, required)

**Returns**: `{"deleted": "scene.<slug>"}` or `{"error": "..."}`.

**Description (for the LLM)**:
> "Remove a saved scene by name. Works on transient scenes
> (created via `save_scene`) and curated scenes loaded from
> scenes.yaml — though the latter come back on next HA restart."

---

### Events

#### `list_recent_events`

Return the last N home events the agent has missed. This is the
**fallback path** for unprompted-event ingestion: until the agent's
notification subscriber is wired up (milestone 8), the agent learns
about motion, door opens, etc. only when it calls this tool. After
milestone 8 it'll also receive notifications in real time, but this
tool stays useful for "what happened earlier?" questions.

**Parameters**:
- `limit` (integer 1–50, optional, default 10)
- `since` (string ISO 8601, optional): only return events after
  this timestamp
- `kinds` (array of strings, optional): filter to specific event
  kinds (see notification shape below)

**Returns**:
```json
{
  "events": [
    {
      "kind": "motion",
      "entity_id": "binary_sensor.hallway_motion",
      "area": "hallway",
      "state": "on",
      "previous_state": "off",
      "timestamp": "2026-05-02T14:32:15+01:00"
    }
  ],
  "count": 3
}
```

**Description (for the LLM)**:
> "List recent significant home events (motion, door / window
> open/close, leak, smoke, lock changes). Use when the user asks
> 'what happened' or 'has there been any activity'. Default
> returns the last 10 events; pass `limit` for more, `since` for
> a time window, or `kinds` to filter by event type."

---

## 3. MCP notifications (server → agent push)

When a significant home event fires, the MCP server emits a
notification on every active session.

**Method**: `notifications/home_event`

**Params shape**:
```json
{
  "kind": "motion" | "contact_open" | "contact_close" | "leak" |
          "smoke" | "lock_changed" | "vacuum_state",
  "entity_id": "binary_sensor.hallway_motion",
  "area": "hallway",
  "state": "on",
  "previous_state": "off",
  "timestamp": "2026-05-02T14:32:15+01:00",
  "attributes": { /* ... entity-specific extras ... */ }
}
```

### What counts as a "significant" event?

| Kind            | Trigger                                                   |
| --------------- | --------------------------------------------------------- |
| `motion`        | A motion `binary_sensor` flips to `on`                    |
| `contact_open`  | A contact `binary_sensor` flips to `on` (door/window open) |
| `contact_close` | A contact `binary_sensor` flips to `off`                  |
| `leak`          | The leak `binary_sensor` flips to `on`                    |
| `smoke`         | The smoke `binary_sensor` flips to `on`                   |
| `lock_changed`  | A `lock` entity changes state                             |
| `vacuum_state`  | The vacuum's `state` changes (docked/cleaning/returning)  |

Not significant (and therefore not pushed): light brightness
changes, temperature drift, power-meter updates, blind position
during animation. These are visible to the GUI via HA WebSocket
subscription but the agent doesn't need them; including them
would just spam the model's context.

### Subscription model

MCP doesn't require explicit subscription for notifications — once
the client has an open session, all server-initiated notifications
on that session arrive. The agent's MCP client opens one persistent
session at registration time and keeps it open.

If the connection drops, the agent's lazy-retry logic re-opens it on
the next chat turn. Events that fire during the gap are picked up
the next time the agent calls `list_recent_events`.

---

## 4. What this contract intentionally omits

These are out of scope for v0.1, listed here so we don't
re-litigate every milestone:

- **Scenes** as first-class entities. We could add an
  `activate_scene("goodnight")` tool later. For now, a "scene" is
  whatever sequence of tool calls the agent decides on. Cleaner
  story for the demo: the agent composes them.
- **Schedules / automations.** Not the agent's job. HA can do this
  natively.
- **Camera streams.** Out of scope — the Reachy + VLM tools cover
  the camera story already.
- **Energy history.** `sensor.power_meter` reports current value
  only; no rolling history tool.
- **User identity.** Single home, no multi-user.
- **`activate_scene` macro tool.** Could be added later as a
  composed tool (similar to `describe_what_you_see` in
  `home_agent_perf`); not in v0.1.

If the demo narrative needs any of these, we'll add them
deliberately in a v0.2 update with version bump.

---

## 5. Tool name conflict check (vs `home_agent_perf` built-ins)

The agent already registers these names:

`get_current_time`, `remember`, `recall`, `list_memories`,
`set_goal`, `clear_goal`, `web_search`, `fetch_url`,
`analyze_workout_clip`, `look_at`, `look_at_angle`, `wake_up`,
`sleep`, `capture_image`, `record_clip`, `start_person_tracking`,
`stop_person_tracking`, `get_tracking_status`, `describe_scene`,
`read_scene`, `detect_object`, `describe_what_you_see`,
`read_what_you_see`, `scan_for_object`.

Our twelve smart-home names — `list_areas`, `list_devices`,
`get_device_state`, `turn_on`, `turn_off`, `set_light`, `lock`,
`unlock`, `set_cover_position`, `set_climate`, `media_control`,
`vacuum_control`, `list_recent_events` — **do not collide**. (Note:
our `sleep` would collide with Reachy's `sleep`, so we don't have a
sleep tool. Same reasoning for not having a generic `play` — could
collide with media tooling later.)

---

## 6. Versioning

- **v0.1**: initial surface, twelve tools + one notification kind.
- **v0.2** (this doc): adds the scenes family — `apply_scene`,
  `apply_states`, `save_scene`, `delete_scene`. Scene domain added
  to `list_devices`. The `media_control` tool name remains reserved.
  Curated scenes ship in `ha-config/scenes.yaml`.
- Breaking changes (rename a tool, change a return shape, drop a
  parameter) bump the minor version.
- Additive changes (new tool, new optional parameter, new
  notification `kind`) are non-breaking and don't bump.

The MCP server includes the contract version in its
`server.info.version` so the client can log a warning on mismatch.

---

## 7. Reference

- Architecture narrative: `docs/architecture.md`
- Agent registration spec (the *other* protocol the agent speaks):
  `home_agent_perf/docs/AGENT_INTEGRATION_SPEC.md`
- Home Assistant entity / area / domain conventions:
  https://www.home-assistant.io/docs/configuration/entity_registry/
  (we follow these exactly)
