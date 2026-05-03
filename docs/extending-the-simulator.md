# Extending the simulator

This dev kit is meant to be hacked on. Adding a new device type,
exposing a new agent tool, defining a new scene, or surfacing a new
event kind — each is a small, well-defined change touching three
or four files at most. This doc walks through the common ones.

If you're using a coding assistant (Claude Code, Cursor, etc.),
read this with [`AGENTS.md`](../AGENTS.md) — together they give the
assistant enough context to make these edits with minimal hand-holding.

> **Adding a *new instance* of an existing device class** (another
> light, another motion sensor) doesn't belong here — that's a
> JSON edit to your workdir's `topology.json`, no Python required.
> See [`your-devices.md`](your-devices.md). This doc covers adding new
> device *classes* (a fan, a humidifier, a media player), which
> requires Python code inside the package.

---

## Add a new device type to the simulator

End-to-end example: a smart fan in the bedroom.

### 1. Pick the HA domain

What kind of entity does HA treat this as? A fan is `fan.*`. Dimmable
or RGB lights are `light.*`. A door sensor is `binary_sensor.*` with
`device_class: door`. Reuse existing types where possible — a fan
that's just on/off is structurally a `switch`. Only invent a new
device class when HA has a real domain for it.

For this example, `fan` is a real HA domain with its own MQTT
discovery schema, so we'll do it properly.

### 2. New module under `src/sandcastle_sim/simulator/`

```python
# fans.py
import json, logging
from typing import Any, Dict
from .base import Device

log = logging.getLogger(__name__)


class Fan(Device):
    domain = "fan"

    def __init__(self, mqtt, spec):
        super().__init__(mqtt, spec)
        self.state = {"state": "OFF", "percentage": 0}

    def discovery_extras(self) -> Dict[str, Any]:
        return {
            "schema": "json",
            "command_topic": self.command_topic,
            "percentage_command_topic": self.command_topic,
            "percentage_state_topic": self.state_topic,
            "value_template": "{{ value_json.state }}",
            "percentage_value_template": "{{ value_json.percentage }}",
            "speed_range_min": 0,
            "speed_range_max": 100,
        }

    async def handle_command(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="ignore").strip()
        try:
            cmd = json.loads(text)
        except json.JSONDecodeError:
            cmd = {"state": text.upper()}
        if "state" in cmd:
            self.state["state"] = str(cmd["state"]).upper()
        if "percentage" in cmd:
            self.state["percentage"] = int(cmd["percentage"])
            if self.state["percentage"] > 0:
                self.state["state"] = "ON"
        await self.publish_state()
        log.info("%s -> %s @ %d%%", self.unique_id,
                 self.state["state"], self.state["percentage"])
```

Pattern: subclass `Device`, set `domain`, initialise `state`,
implement `discovery_extras()` (the discovery payload's
domain-specific fields) and `handle_command()` (react to MQTT
messages on `command_topic`).

### 3. Wire the domain into the topology loader

The simulator's topology lives as JSON. The *bundled* topology
(``data/seeds/topology.json``) and any user override
(``<workdir>/.sandcastle/topology.json``) share the same schema.
For a brand-new domain like `fan`, two small touches:

**a. Expose a `FANS` constant from `topology.py`.** Add one line
alongside the existing `LIGHTS`, `SWITCHES`, … so callers (the
simulator's `main.py`) can iterate it:

```python
FANS: list[DeviceSpec] = list(_DEVICES.get("fan", []))
ALL_BY_DOMAIN["fan"] = FANS
```

**b. Add a default fan to the bundled seed** so the demo home picks
up an example. Edit `data/seeds/topology.json`, append a `"fan"`
key under `devices`:

```json
"fan": [
  {"slug": "bedroom_fan", "area": "bedroom", "name": "Bedroom Fan"}
]
```

The `slug` becomes the entity_id slug after slugification of `name`
(see `light.bedroom_main` → `Bedroom Main` for the pattern).

> **Note:** users who *just* want to drop a fan into their existing
> home (without modifying the package's seed) edit
> `<workdir>/.sandcastle/topology.json`. The package edit is for
> shipping the new class as part of the bundled demo.

### 4. Wire it in `main.py`

```python
from .fans import Fan
from .topology import FANS

# In _build_devices:
for spec in FANS:
    devices.append(Fan(mqtt, spec))
```

### 5. (Optional) Floor-plan rendering

Two pieces — *placement* (data) and *appearance* (code):

**Placement.** Edit your workdir's
`<workdir>/.sandcastle/floorplan.json` and add an entry under `devices`:

```json
"fan.bedroom_fan": { "area": "bedroom", "type": "fan", "x": 200, "y": 120 }
```

`x`/`y` are room-local pixel coordinates; the validator rejects
out-of-room positions. Or skip the manual edit and run
`sandcastle-sim floorplan auto` once the entity is live in HA — it
will be placed by type with deterministic priors. See
[`docs/floorplan.md`](floorplan.md).

**Appearance.** In `src/sandcastle_sim/data/gui/index.html`:

- Add a renderer in `updateDevice`'s dispatch
  (`if (t === "fan") renderFanBody(ent, state)`).
- Add a `renderFanBody` function that draws a fan icon based on
  state/percentage.

If you skip the GUI step, the fan still works for the agent — it
just won't appear on the floor plan.

### 6. Restart and verify

```bash
make wipe-sim       # clear stale MQTT discovery messages
make run-sim        # restart simulator
```

Check HA picks up the new entity:

```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" \
  http://localhost:8123/api/states/fan.bedroom_fan
```

Now your agent can see and control it. `list_devices` returns the
fan with `domain: "fan"`. The smart-home MCP server's `turn_on` /
`turn_off` won't directly work for `fan.*` (those are scoped to
`light` / `switch` in `server.py`); add a control tool.

---

## Add a new MCP tool

End-to-end example: a `set_fan` tool.

### 1. Update the tool contract

Edit `docs/tool-contract.md` first. Document the tool's name,
parameters, return shape, error conventions. Bump the version (the
contract is the spec; the code follows it). This means anyone
integrating their own agent can read the new tool's signature
before you finish the implementation.

### 2. Add the tool function

In `src/sandcastle_sim/mcp_server/server.py`:

```python
@mcp.tool()
async def set_fan(
    entity_id: str,
    percentage: int,
) -> Dict[str, Any]:
    """Set a fan's speed from 0 (off) to 100 (full).

    ``entity_id`` MUST be an exact fan entity ID such as
    ``fan.bedroom_fan``. Use ``list_devices(domain='fan')`` if unsure.
    Setting percentage to 0 turns the fan off.
    """
    if not entity_id:
        return {"error": "entity_id is required"}
    if _domain(entity_id) != "fan":
        return {"error": f"set_fan requires a fan entity_id, got {entity_id!r}"}
    try:
        pct = max(0, min(100, int(percentage)))
    except (TypeError, ValueError):
        return {"error": f"percentage must be int 0-100, got {percentage!r}"}

    ha = await _get_ha()
    try:
        if pct == 0:
            await ha.call_service(
                "fan", "turn_off", service_data={"entity_id": entity_id})
        else:
            await ha.call_service(
                "fan", "set_percentage",
                service_data={"entity_id": entity_id, "percentage": pct})
    except RuntimeError as exc:
        return {"error": f"HA service call failed: {exc}"}
    return await _post_control_state(ha, entity_id)
```

Conventions:

- Validate inputs eagerly. Return `{"error": "..."}` for bad
  arguments — never raise.
- Map onto HA's service registry (the `ha.call_service` helper).
- Return the entity's resulting state via `_post_control_state` so
  the agent can self-confirm.
- Match the tone of existing tools' descriptions. They're the
  primary signal for which tool the agent picks.

### 3. (Optional) Voice announcement

If your reference agent has voice mode (the
[`home_agent_perf`](https://github.com/itsMarco-G/home_agent_perf)
sibling does), add a phrase in
`home_agent_perf/app/tool_loop.py:tool_announcement`:

```python
if name == "set_fan":
    spoken = _spoken_entity(a.get("entity_id")) or "fan"
    pct = a.get("percentage")
    if pct == 0:
        return f"Reachy is turning off the {spoken}."
    return f"Reachy is setting the {spoken} to {pct} percent."
```

### 4. Restart MCP server, verify

```bash
# Restart the MCP server. The new tool appears in list_tools.
.venv/bin/python scripts/smoketest_mcp.py
```

The agent will see the tool on its next chat turn (lazy
re-registration handles this; no agent restart needed).

---

## Add a new curated scene

Easiest of all. Edit `ha-config/scenes.yaml`:

```yaml
- id: bedtime
  name: Bedtime
  icon: mdi:bed-clock
  entities:
    light.bedroom_main:
      state: off
    light.bedroom_mood:
      state: on
      brightness: 25
      rgb_color: [255, 100, 50]
    cover.bedroom_blind:
      state: closed
      current_position: 0
    fan.bedroom_fan:
      state: on
      percentage: 30
```

Restart HA so it reloads scenes:

```bash
make restart
```

The agent picks up the new scene on its next `list_devices` call
(and the cheat sheet section refreshes when the agent's smart-home
registrar runs).

---

## Add a new event kind

End-to-end example: a `temperature_alert` event when bedroom
temperature crosses a threshold.

### 1. Extend the classifier

In `src/sandcastle_sim/mcp_server/events.py`:

```python
def classify(new_state, old_state):
    # ... existing rules ...

    if (new_state.get("entity_id", "").startswith("sensor.")
        and (new_state.get("attributes") or {}).get("device_class") == "temperature"):
        try:
            new = float(new_state.get("state"))
            old = float(old_state.get("state")) if old_state else None
        except (TypeError, ValueError):
            return None
        if old is not None and old <= 25 < new:
            return "temperature_alert"

    return None
```

### 2. Document in the contract

Add the new kind to `docs/tool-contract.md` §3:

```markdown
| `temperature_alert` | A temperature sensor crossed 25°C upward    |
```

### 3. Update the consumer (optional)

If you're consuming events on the agent side, add UI rendering for
the new kind in `home_agent_perf/static/main.js` (CSS class
`he-temperature_alert`, an entry in `EVENT_KINDS` if you want it
in the recent-events panel).

---

## What NOT to do

- **Don't add Python state to per-turn paths in the agent.** The
  agent loop in `tool_loop.py` is read-only against the registry +
  config. Mutable state (memories, goals, recent events) lives in
  `app/agent_state.py` or `app/memory.py`.
- **Don't make tools synchronous-blocking-on-IO without timeouts.**
  The agent's `TOOL_REQUEST_TIMEOUT_S` (default 120 s) is the outer
  bound, but most tools should respond in < 1 s.
- **Don't skip the `family` field on `RegisteredTool`** in the
  reference agent. The dynamic tool router (in
  `home_agent_perf/app/tool_routing.py`) uses it to filter which
  schemas hit the model per turn.
- **Don't break the contract silently.** Bump the version in
  `docs/tool-contract.md` whenever you change a tool's signature
  or return shape. Other consumers may pin against it.

---

## See also

- [`AGENTS.md`](../AGENTS.md) — repo conventions and orientation
- [`docs/architecture.md`](architecture.md) — system design
- [`docs/tool-contract.md`](tool-contract.md) — canonical tool surface
- [`docs/integrating-your-agent.md`](integrating-your-agent.md) —
  hooking your agent up
