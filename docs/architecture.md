# Smart Home Demo — Architecture

This is the demo's foundational document. Read it before touching code.

The point of the demo is to show an LLM agent driving a realistic
smart home through standard protocols, with a live floor-plan view
that reacts to the agent's actions and to autonomous device events.
The architecture is deliberately representative of real smart-home
stacks (MQTT + Home Assistant + MCP) so the story scales beyond the
demo: swapping in a real Matter device or pointing the agent at a
real HA instance is a config change, not a rewrite.

---

## 1. Big picture

```
+-------------------------------+
|  home_agent_perf              |
|  (existing repo, separate)    |
|                               |
|  Tool registry                |
|   - in-process built-ins      |
|   - HTTP backends (workout,   |
|     reachy, vlm)              |
|   - smart_home (NEW: MCP)  ◄──+──── MCP (streamable HTTP, JSON-RPC 2.0)
+-------------------------------+    |
                                     |
+----------------------------------------------+
|  smart_home_demo (THIS REPO)                 |
|                                              |
|  +-------------------------------+           |
|  |  Smart Home MCP Server        |◄----------+
|  |  (FastMCP, streamable HTTP)   |
|  |                               |
|  |  - tools/list  (discovery)    |
|  |  - tools/call  (control)      |
|  |  - notifications/* (events)   |
|  +---------------+---------------+
|                  | Home Assistant WebSocket API
|                  ▼
|  +-------------------------------+
|  |  Home Assistant Core          |  (Docker)
|  |  - areas, entities, services  |
|  |  - MQTT integration           |
|  |  - WebSocket + REST           |
|  +---------------+---------------+
|                  | MQTT
|                  ▼
|  +-------------------------------+
|  |  Mosquitto broker             |  (Docker)
|  +---------------+---------------+
|                  | MQTT (HA discovery convention)
|                  ▼
|  +-------------------------------+
|  |  Device Simulator             |  (Python process)
|  |  - lights / locks / climate / |
|  |    sensors / blinds / vacuum  |
|  +-------------------------------+
|
|  +-------------------------------+
|  |  Floor Plan GUI               |  (browser, single HTML file)
|  |  - HA WebSocket subscription  |
|  |  - SVG floor plan             |
|  +-------------------------------+
+----------------------------------------------+
```

Five processes total. Two run in Docker (Mosquitto, HA Core); three
run natively (MCP server, simulator, GUI is just a static file).

---

## 2. Layering principles

These are load-bearing — violating them breaks the extensibility
story.

### 2.1 The MCP server is the only thing that speaks home-control

The simulator publishes device state to MQTT. HA aggregates it. The
MCP server reads from HA and writes through HA. **Nothing outside the
simulator touches MQTT directly.** Nothing outside HA touches device
protocols directly.

Why: this is what makes Matter painless to add later. A Matter device
appears as an HA entity exactly like an MQTT one. Code above HA
doesn't notice the protocol underneath; the only visible difference
is the `protocol` attribute on each device, which the GUI uses to
render a small badge.

### 2.2 The GUI talks to HA, not to the MCP server

The floor plan subscribes to HA's WebSocket API for live entity-state
events. It does not go through the MCP server. Two reasons:

- HA's event firehose is exactly what a real HA dashboard subscribes
  to. Showing the GUI driven by it makes the demo more honest.
- It keeps the MCP server focused on tool semantics (one event per
  meaningful change for the agent) rather than UI semantics (every
  byte-change for visual smoothness).

Different consumers, different views, same source of truth.

### 2.3 The agent uses MCP; other tool families stay where they are

`home_agent_perf` already has three HTTP-backed tool families
(workout, Reachy, VLM) using its homegrown protocol from
`docs/AGENT_INTEGRATION_SPEC.md`. We do **not** churn those.

Smart-home is the first MCP citizen in the agent. We add one new tool
module — `app/tools/smart_home.py` plus `app/tools/mcp_client.py` —
that registers MCP-discovered tools into the same `ToolRegistry`. The
agent loop in `tool_loop.py` doesn't change at all; it dispatches
MCP-backed tools the same as in-process ones.

This is also a story for the demo: same agent, four wire formats,
side by side. MCP is the one we didn't have to invent a contract for.

### 2.4 The MCP server holds no device state

State lives in HA. The MCP server is a stateless adapter — every tool
call resolves entities and reads state through HA's WebSocket API in
real time. This means:

- Restarting the MCP server doesn't lose anything.
- The GUI and the agent are always reading the same source of truth.
- Adding a real HA instance later is a base-URL change.

The only state the MCP server keeps in memory is its **subscriber
list** for MCP notifications, and that's transient.

---

## 3. Process layout and Docker decision

| Process              | Where        | Why                                                                 |
| -------------------- | ------------ | ------------------------------------------------------------------- |
| Mosquitto            | Docker       | Avoids polluting host with broker config; standard image            |
| Home Assistant Core  | Docker       | Same — HA config bootstrap is finicky outside a container           |
| Smart Home MCP server | Native (Python) | Fast iteration; no networking gotchas; stdlib + `mcp[cli]`       |
| Device simulator     | Native (Python) | Fast iteration; needs to publish to broker on `localhost:1883`   |
| Floor plan GUI       | Native (browser) | Single HTML file, no build step, opens directly                  |

`docker-compose.yml` covers Mosquitto + HA. A `Makefile` brings up
the Docker pieces, then the native ones, in the right order.

Both Docker services bind to `localhost` only (the LAN-trust default
the existing `home_agent_perf` services use). HTTPS isn't part of
the demo.

---

## 4. The four flows in detail

### 4.1 Discovery (agent boot or lazy retry)

```
home_agent_perf process boot
└─► state.py:_build_registry() runs every registrar
    └─► register_smart_home_tools(registry, smart_home_mcp_url, timeout_s)
        └─► MCP client opens streamable-HTTP session
            └─► tools/list  →  list of tool schemas
        └─► for each tool schema:
            └─► RegisteredTool(name, schema, handler=mcp_call_handler)
            └─► registry.register(...)
        └─► open background task: subscribe to notifications, push
            them onto the agent's event queue
```

If the MCP server is offline at boot the registrar returns `[]`
(matching `register_reachy_tools` behavior). Each chat turn re-runs
`ensure_smart_home_tools_registered()` with the standard 3 s
cooldown until the server comes up.

### 4.2 Tool call (agent decides to act)

```
LLM emits tool_call: turn_on  args={entity_id: "light.kitchen_main"}
└─► tool_loop.py dispatches via registry → mcp_call_handler(args)
    └─► mcp_client.call_tool("turn_on", args)
        └─► JSON-RPC tools/call over streamable HTTP to MCP server
            └─► sandcastle_sim/mcp_server/server.py: turn_on() handler
                └─► HA WebSocket: services/light/turn_on entity_id=...
                └─► await state change confirmation
            └─► returns {entity_id, state, attributes, ...}
        └─► JSON-RPC response to client
    └─► yield ("result", {...})
└─► agent loop appends tool result to history, continues
```

### 4.3 Live device update (autonomous, e.g. motion sensor)

```
Simulator publishes MQTT to homeassistant/binary_sensor/motion_living_room/state
└─► HA receives, updates entity, fires state_changed event
    ├─► HA WebSocket subscribers (the GUI is one) receive the event
    │   └─► GUI re-renders the icon (motion glow, etc.)
    └─► The MCP server is also subscribed to relevant state_changed
        events. For events the agent should know about (motion,
        contacts, smoke, leak), it emits an MCP notification:
        └─► notifications/home_event {type: "motion", entity_id: ...}
            └─► home_agent_perf MCP client receives the notification
                └─► (option B from design): pushed onto the agent's
                    event queue as an unprompted-event source
```

Note: the unprompted-event ingestion path on the `home_agent_perf`
side is **future work** referenced in `app/agent_state.py` ("Phase 3
reminders will reuse this module for the unprompted-event subscriber
list"). For this demo we wire it up in the agent in milestone 8.
Until then the agent learns about events by calling the
`list_recent_events` tool when the user asks.

### 4.4 GUI render loop

```
Browser opens index.html
└─► JS opens HA WebSocket, authenticates with long-lived token
    └─► subscribe to state_changed events
    └─► fetch /api/states (initial snapshot)
└─► For each entity:
    ├─► look up its area → place icon in the right room of the SVG
    ├─► render device icon by domain (light, lock, etc.)
    └─► attach state listeners to update visuals on change
└─► Side panel: show last 10 events (state changes for sensors and
    actions, suppressed for noisy attribute updates)
```

The GUI is a passive display. No user controls. Everything visible
on screen reflects what the simulator + HA + agent are doing.

---

## 5. Where Matter fits later

The architecture is pre-built for Matter and it slots in at one
place:

```
                    Home Assistant Core
                          │
        ┌─────────────────┼──────────────────┐
        │                 │                  │
        ▼                 ▼                  ▼
   MQTT integration   matter_server       (others)
        │              integration
        ▼                 ▼
   Mosquitto       python-matter-server
        │                 │
        ▼                 ▼
   simulator        real Matter device
                    (commissioned via HA UI)
```

`docker-compose.yml` includes a commented-out `matter-server` service
with a note. `docs/adding-matter.md` walks through enabling it,
commissioning a device, and verifying that the GUI shows the device
with `protocol: matter` instead of `protocol: mqtt`.

**No code in the MCP server, simulator, or GUI changes** when this
happens. That's the whole point. The MCP tools read entity attributes
that include `protocol` (set by us during entity setup, defaulting to
`mqtt`); a Matter device gets `protocol: matter` and everything else
is identical.

---

## 5b. Where HA-native alert rules would fit later

Today the classification of "significant events" — door open vs.
close, leak detected, lock state changed, smoke alarm — happens in
Python inside the smart-home MCP server (`events.py:classify()`).
For the v0.1/v0.2 demo this is fine: ~30 lines of obviously-correct
filter logic, easy to read, easy to test, no external dependencies.

In a real HA installation the same logic typically lives in
`automations.yaml` (or the HA UI's Automations editor), not in
client code. Each automation has a trigger ("any binary_sensor with
device_class=moisture transitions to on"), optional conditions ("and
the home is in `away` mode"), and an action: fire a custom HA event
like `home_alert` with a `kind` payload. External consumers — the
MCP server, a mobile dashboard, a notification daemon — subscribe
only to that single event type and never see the noisy
`state_changed` firehose.

Migration when this becomes the right move:

```
state_changed firehose                 home_alert custom events
  |                                       ^
  v                                       |
[ events.py:classify ]    -- becomes -->  [ automations.yaml ]
  |                                       |
  v                                       v
[ EventBuffer + SSE ]                   [ EventBuffer + SSE ]
                                          (same; one-line subscribe
                                           filter change)
```

Concretely: each `kind` in `events.py` becomes one HA automation.
The MCP server's HA WS subscription changes from `event_type:
state_changed` to `event_type: home_alert`, and `classify()`
becomes a no-op (the kind comes from the automation's payload).
Estimated ~1.5–2 hours of work; ~3 automations in YAML and a few
lines deleted from `events.py`.

When to actually do it:

- **Composite alerts arrive** (intruder = motion + contact_open +
  no person home; guest arrived = doorbell + camera detected face).
  These are awkward in Python state-tracking but native to HA's
  trigger/condition/action model.
- **User-tunable rules become a feature** (don't alert during
  10 AM – 5 PM; only alert when away). HA's UI exposes time-of-day
  conditions; building the same surface in Python is busywork.
- **A second consumer of the alert stream appears** (mobile app
  notifications, secondary dashboard). Centralising in HA means
  one place fires, many places listen.

Until any of those are true, the Python classifier is the simpler
choice and we keep it. The architecture is deliberately structured
so the migration is local — no API changes elsewhere.

---

## 6. Where MCP fits in `home_agent_perf`

Two new files:

- `app/tools/mcp_client.py` — a thin wrapper over the `mcp` Python
  SDK's streamable-HTTP client. Exposes `MCPClient.connect(url)`,
  `MCPClient.list_tools()`, `MCPClient.call_tool(name, args)`, and a
  `subscribe_notifications(callback)` helper.
- `app/tools/smart_home.py` — the registrar. Uses `mcp_client.py` to
  fetch the tool list and register each one, mirroring the shape of
  `app/tools/reachy.py` (lazy retry, cooldown, list-of-names return).

One config addition in `app/config.py:SETTINGS` (per CLAUDE.md §3):

- `SMART_HOME_MCP_URL` (string, default `http://localhost:8765/mcp/`)

One call from `app/state.py:_build_registry()`:

- `register_smart_home_tools(registry, config.smart_home_mcp_url,
  config.tool_request_timeout_s)`

Plus the standard `ensure_smart_home_tools_registered()` lazy-retry
hook called from each chat turn.

The `mcp` SDK adds one dependency to `requirements.txt`. The CLAUDE
guidance ("no new dependencies without justification") is satisfied:
MCP is the protocol the demo is built around, and the SDK is the
canonical client.

---

## 7. Versioning and stability

- **Tool surface**: the MCP server's tool names and signatures are
  the contract documented in `docs/tool-contract.md`. Changing them
  is a breaking change — bump the contract version.
- **Notifications**: the `home_event` notification shape is part of
  the contract too. Same rule.
- **HA WebSocket API**: HA's API is what it is. We pin the HA Docker
  image major version; minor upgrades are usually fine.
- **MCP protocol version**: pinned in the SDK we use.

The contract doc is the source of truth. This file is just the
narrative.

---

## 8. What this architecture does NOT include

For honesty:

- **Auth**: LAN-trust default, like every other piece of this stack.
  Production smart-home would need OAuth2 / token auth at HA and
  somewhere on the MCP transport. Out of scope.
- **TLS**: not used. Same reason.
- **Multi-user / multi-home**: one home, one agent.
- **Persistence beyond HA**: HA persists entity registry and area
  assignments. The simulator's runtime state (motion timing, vacuum
  pose) is in-memory and resets on restart, which is fine for a demo.
- **Cloud anything**: everything runs on the demo host.

---

## 9. References

- `docs/tool-contract.md` — the designed MCP tool surface
- `docs/adding-matter.md` — runbook for adding a Matter device later
- `docs/adding-mcp.md` — folded into this doc (MCP is native here,
  no separate "how to add it" because it's not optional)
- `docs/demo-script.md` — the on-stage flow
- `home_agent_perf/CLAUDE.md` §4 — registrar pattern we mirror
- `home_agent_perf/docs/AGENT_INTEGRATION_SPEC.md` — the *other*
  protocol the agent speaks (custom HTTP), for context
