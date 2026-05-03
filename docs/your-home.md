# Customise your home

When `sandcastle-sim start` succeeds you've got a copy of *someone
else's* apartment running — six rooms, twenty-two simulated devices,
a stylised blueprint floor plan. This walkthrough is how you make
it yours.

The pattern is "**ask your coding agent, verify in the browser**":
each task below is a prompt you paste into Claude Code / Cursor /
Codex / Copilot in the project root. The agent reads files, makes
edits, runs commands. You hard-refresh `http://localhost:8766` and
see the change.

If you don't have a coding agent set up, you can do every step by
hand — the doc points at the files. But the design here is "tell the
agent, see it happen." That's the muscle the kit is built around.

About 15 minutes for the whole thing.

---

## Prerequisites

Run a quick status check:

```sh
sandcastle-sim status
```

All five rows should be `● UP`. If anything's down, `sandcastle-sim
start` brings the foreground processes back; if HA or Mosquitto are
down, see [`docs/architecture.md`](architecture.md).

Open the floor plan in your browser:

```
http://localhost:8766
```

Click any device — a light toggles, a contact sensor flips, a lock
clicks. That's the home you're about to customise.

Open your coding agent in the project root. Optional but recommended:
have it read [`AGENTS.md`](../AGENTS.md) once, so the rest of this
doc doesn't repeat orientation.

---

## Task 1 — Tour your stack

The agent's first job is to know what it's looking at.

**Prompt:**

> Read `AGENTS.md` and skim `docs/floorplan.md`. Then connect to the
> running MCP server at `http://localhost:8765/mcp/`, call
> `list_devices`, and give me a short summary: how many devices per
> room, which agent tools are available, and one or two devices you'd
> recommend toggling first to verify the agent control path works.
> Under 200 words.

**What you should see:**

The agent reads two files, runs an MCP call, and reports back.
Something like *"6 rooms, 22 devices, tools include `turn_on` /
`set_light` / `lock` / `list_devices` / `start_vacuum` …; for a quick
end-to-end check I'd toggle `light.kitchen_counter` — plain dimmable,
visually obvious."*

Try the suggestion in the GUI to confirm the agent is grounded in
something real, not hallucinating.

**What you've learned:** the agent has direct access to live state
through the MCP server. Anything it claims about your home, you can
verify with `list_devices` yourself:

```sh
.venv/bin/python scripts/smoketest_mcp.py
```

---

## Task 2 — Move a device on the floor plan

The floor plan's layout (rooms + per-device coordinates) lives in a
single JSON file. Editing it is a config change, not a code change —
no restart needed.

**Prompt:**

> Move the kitchen counter light to the south wall of the kitchen.
> Use the placement vocabulary in `docs/floorplan.md` (south wall =
> `y` near `room.h - margin`). Edit
> `<workdir>/.sandcastle/floorplan.json` (your home, created on
> first `sandcastle-sim start`). Validate by reloading
> the file with `sandcastle_sim.floorplan.load_floorplan`, and tell me
> when to refresh the browser.

**What you should see:**

- The agent edits exactly one entry in `floorplan.json`. The
  kitchen counter light's `y` becomes ~240 (kitchen height is 270).
- Agent confirms the file still validates.
- Hard-refresh the browser (**Ctrl+Shift+R** / **Cmd+Shift+R**).
  The kitchen counter light has moved south.

**Try a free-form follow-up:**

> Now centre the kitchen window contact horizontally, and move the
> coffee machine next to where the counter light used to be.

Two more single-line edits. Refresh; both moved.

**What you've learned:** floor-plan layout is data. The agent edits
it without touching any rendering code. The validator catches
mistakes — try *"move the coffee machine to (-99, -99)"* and the
agent (or the validator) will refuse with a clear error.

---

## Task 3 — Add a real device to your home

The bathroom in the bundled home is empty. Let's give it a ceiling
light. This is the full add-a-device flow:

1. Add a spec to your home's topology in the workdir.
2. Restart the simulator so MQTT discovery publishes the new entity
   to HA.
3. Run `sandcastle-sim floorplan auto` so the new device gets a
   deterministic position on the floor plan.
4. Hard-refresh and verify the agent can control it.

Your home — both the floor plan and the simulator's device list —
lives in `<workdir>/.sandcastle/`, **not** in the package source.
That's how customisations survive `git pull` and `pip install
--upgrade`. The package's `data/seeds/` directory holds read-only
defaults; your workdir copy is editable and durable. (See
[`floorplan.md` § Persistence](floorplan.md#persistence).)

**Prompt:**

> Add a "Bathroom Main" dimmable ceiling light to my home.
> Entity_id should be `light.bathroom_main`, area `bathroom`.
>
> Steps:
> 1. Read `docs/extending-the-simulator.md` and `docs/floorplan.md`
>    to confirm conventions.
> 2. Edit `.sandcastle/topology.json` (in the project root). Append
>    one entry to `devices.light`:
>    `{"slug": "bathroom_main", "area": "bathroom", "name": "Bathroom Main", "kind": "dimmable"}`.
> 3. Restart the foreground processes so the simulator re-publishes
>    MQTT discovery: `sandcastle-sim stop && sandcastle-sim start`.
>    Wait for the stack to come back up; verify with
>    `sandcastle-sim status`.
> 4. Run `sandcastle-sim floorplan auto`. By default it places only
>    new devices and leaves existing positions untouched, which is
>    what we want here.
> 5. Confirm HA sees the new entity:
>    `curl -s -H "Authorization: Bearer $HA_TOKEN" http://localhost:8123/api/states/light.bathroom_main`
> 6. Tell me to hard-refresh, and try toggling the new icon.

**What you should see:**

- One new entry appended in `.sandcastle/topology.json`.
- Stack cycles cleanly (~10 seconds).
- `floorplan auto` reports `1 device placed (existing kept,
  1 newly laid out)`.
- HA returns a JSON state for `light.bathroom_main` (state probably
  `"off"`).
- After hard-refresh: the bathroom now has a light icon. Click it —
  it toggles. Click again — it toggles back.

**Verify the agent can control it.** In a separate terminal:

```sh
sandcastle-sim "turn on the bathroom light"
```

The agent picks `turn_on` with entity_id `light.bathroom_main`, the
icon glows in the GUI, and the agent confirms in chat.

**What you've learned:** adding a device end-to-end is two small
edits to your workdir — `topology.json` (which devices exist) and
`floorplan.json` (where they sit; generated for you by
`floorplan auto`). The package source was never touched. Nothing
above HA changed: not the MCP server, not the agent, not the GUI's
renderer. That's the same path a real Matter device follows when
commissioned ([`adding-matter.md`](adding-matter.md)).

> **Note:** this task adds a *new instance* of an existing device class
> (`Light`). To add a wholly new *class* (e.g. a fan, a humidifier),
> you need to write Python code inside the package — see
> [`extending-the-simulator.md`](extending-the-simulator.md). That's
> a developer flow, not a workdir-edit flow.

---

## Where to go next

You've now exercised every customisation surface this kit exposes.
From here:

- **Add more devices.** Pick anything from
  [`extending-the-simulator.md`](extending-the-simulator.md). A fan
  is the canonical example; it also walks through adding a new MCP
  tool (`set_fan`) so the agent can control speed.
- **Add a curated scene.** Edit `ha-config/scenes.yaml` (e.g. a
  "bathtime" scene that turns on your new bathroom light at 30%) and
  ask the agent *"set up bathtime"*.
- **Run the eval suite.** `sandcastle-sim eval` runs the bundled
  `quick.yaml`. Add a sixth case that uses your new bathroom light:
  saves you from regressions when you change the agent's prompts or
  models.
- **Plug in a real device.** [`adding-matter.md`](adding-matter.md)
  is the runbook. The `floorplan auto` flow you just used places it
  for you once HA has commissioned it.

---

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| GUI didn't change after a refresh | Browser cache. Hard-refresh: **Ctrl+Shift+R** / **Cmd+Shift+R**. |
| `floorplan auto` says "Could not reach the stack" | MCP server isn't up. `sandcastle-sim status` to check, `sandcastle-sim start` to revive. |
| Agent edited a file but the GUI still looks wrong | Two layers — config (`floorplan.json`) reloads on browser refresh; rendering code (`index.html`) needs a sim restart. If only the JSON changed, just hard-refresh. |
| New device doesn't appear in HA | The simulator publishes MQTT discovery on startup; if you added a spec but didn't restart, HA hasn't seen it yet. `sandcastle-sim stop && sandcastle-sim start`. |
| Schema-validation error on `floorplan.json` | The validator is strict on purpose — out-of-room positions, unknown areas, missing types all fail. Read the error; ask the agent to fix it; re-validate. |

---

## See also

- [`AGENTS.md`](../AGENTS.md) — coding-agent orientation for the
  whole repo.
- [`docs/floorplan.md`](floorplan.md) — schema + agent vocabulary
  for floor-plan edits.
- [`docs/extending-the-simulator.md`](extending-the-simulator.md) —
  full reference for adding device types, MCP tools, scenes, events.
- [`docs/architecture.md`](architecture.md) — what runs where and
  why; read once, refer back.
