# Customise your devices

When `sandcastle-sim start` succeeds you've got a copy of *someone
else's* apartment running — six rooms, twenty-two simulated devices,
a stylised blueprint floor plan. This walkthrough is how you make
the *devices* yours: tour what's there, move things around, and add
your own.

For customising the floor plan itself (rooms, layout, swapping in
an image of your real home), see
[`your-floorplan.md`](your-floorplan.md).

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

Open your coding agent in the project root. **First thing to do: ask
it to read [`AGENTS.md`](../AGENTS.md).** That file contains the
mechanics — file paths, the MCP URL, the placement vocabulary, the
restart procedure, the verification protocol — so the prompts below
can stay focused on *what* you want, not *how* to do it.

> "Read `AGENTS.md` so you have the conventions for this repo,
> including the section on customising the user's home."

---

## Task 1 — Tour your stack

**Prompt:**

> Tour my smart-home stack — what's running, what devices do I have
> across which rooms, and what's a good first thing to test if I want
> to confirm the agent control path actually works?

**What you should see:**

The agent connects to the MCP server, calls `list_devices`, and
gives you something like *"6 rooms, 22 devices, tools include
`turn_on` / `set_light` / `lock` / `list_devices` / `start_vacuum`;
for a quick end-to-end check I'd toggle `light.kitchen_counter` —
plain dimmable, visually obvious."*

Try the suggestion in the GUI to confirm the agent is grounded in
something real, not hallucinating.

**What you've learned:** the agent has direct access to live state.
Anything it claims about your home, you can verify with
`list_devices` yourself:

```sh
.venv/bin/python scripts/smoketest_mcp.py
```

---

## Task 2 — Move a device on the floor plan

The floor plan's layout (rooms + per-device coordinates) lives in
a single JSON file. Editing it is a config change, not code — no
restart needed.

**Prompt:**

> Move the kitchen counter light to the south wall of the kitchen.

**What you should see:**

- The agent edits exactly one entry in `<workdir>/.sandcastle/floorplan.json`.
  The kitchen counter light's `y` becomes ~240 (kitchen height is 270).
- Agent validates the file, then tells you to hard-refresh the
  browser (**Ctrl+Shift+R** / **Cmd+Shift+R**). The light has
  moved south.

**Try a free-form follow-up:**

> Centre the kitchen window contact horizontally, and move the
> coffee machine next to where the counter light used to be.

Two more single-line edits, one refresh, both moved.

**What you've learned:** floor-plan layout is data the agent edits
without touching any rendering code. Vocabulary like "south wall,"
"swap," "next to" is documented in `AGENTS.md` and `docs/floorplan.md`
— the agent maps your words to coordinates. The validator catches
mistakes; try *"move the coffee machine to (-99, -99)"* and the
agent (or the validator) refuses with a clear error.

---

## Task 3 — Add a real device to your home

The bathroom in the bundled home is empty. Let's give it a ceiling
light.

**Prompt:**

> Add a dimmable ceiling light called "Bathroom Main" to my home.
> When you're done, tell me how to verify it works — I want to be
> able to click it on the floor plan and ask the agent to turn it on.

**What you should see:**

The agent (following the procedure documented in `AGENTS.md`) will:

- Append one entry to `.sandcastle/topology.json` under `devices.light`.
- Restart the simulator so HA picks up the new entity via MQTT
  discovery (`sandcastle-sim stop && sandcastle-sim start`).
- Run `sandcastle-sim floorplan auto` to place the new icon.
- Curl HA to confirm the entity exists.
- Tell you to hard-refresh the browser and try interacting with it.

The whole thing takes ~30 seconds from prompt to clickable icon.

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
- **Use your real floor plan as the GUI background.**
  [`your-floorplan.md`](your-floorplan.md) walks through swapping
  in an image of your actual home. Same agent-driven pattern, one
  task, ~10 minutes.
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
