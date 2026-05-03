# Use your own floor plan

Companion to [`your-devices.md`](your-devices.md). That doc was about
*what's in* your home (devices). This is about *how it's shaped*:
replacing the bundled six-room blueprint with an image of your real
home, or a sketch the agent generates from your description.

By the end you'll have a GUI floor plan that looks like *your* home,
with your devices placed on top. Two paths to get there:

- **Path A — Sketch from a description.** You don't have a floor
  plan image; you just describe your home in words. The agent
  generates a simple SVG floor plan and uses it as the GUI
  backdrop. Quick, schematic, ~5 minutes.
- **Path B — Drop in your real image.** You have a floor plan
  (real-estate listing PNG, architect drawing, hand sketch, vacuum
  map, etc.). The agent uses it as the GUI backdrop and aligns
  rooms to what it sees. Realistic, ~5–10 minutes plus a couple of
  iteration rounds.

Pick one. They both edit the same file (`<workdir>/.sandcastle/floorplan.json`)
and both produce a persistent customised home. You can switch between
them later by re-running the other path.

---

## Prerequisites

Same as `your-devices.md`:

- `sandcastle-sim status` shows everything `● UP`.
- A coding agent open in the project root, with
  [`AGENTS.md`](../AGENTS.md) loaded into context.
- The customise-your-devices walkthrough done at least once (so you
  know the prompt-and-refresh rhythm).

Plus, for **Path B**, an image of your floor plan in PNG / JPG / SVG.
What works:

| Works as a backdrop | Doesn't work |
| --- | --- |
| Real-estate listing floor plan | A photo of a room |
| Architect drawing | A 3D rendering at an angle |
| Hand sketch (top-down) | A panorama |
| Roborock / robot vacuum map | A satellite image with no rooms |
| Inkscape / Figma export | |

If your image isn't a top-down floor plan, the agent can render it
as a backdrop but the rooms won't make sense — go with Path A
instead.

---

## Path A — Sketch from a description

Use this path when you don't have a floor plan image. Describe
your home in words; the agent generates a simple sketch and uses
it as the GUI background.

**Prompt:**

> Sketch a floor plan for my home and use it as the GUI background.
> My home is a 1-bedroom apartment: combined kitchen and living
> area, a bedroom, a bathroom, and a small hallway. Tell me what
> you did and when to refresh.

(Tweak the description to match your home — bedrooms, whether
kitchen and living are separate, whether you have a study, etc.
The agent draws what you described.)

**What you should see:**

The agent reports back, naming the SVG it created, the file it
edited, and your refresh cue. After the hard-refresh: the bundled
blueprint is gone; your home's schematic is in its place; your
devices land in their new rooms.

---

## Path B — Use your real floor plan image

Use this path when you already have a floor plan (real-estate
listing PNG, architect drawing, vacuum map, etc.) you'd like as
the GUI background.

**Step 1.** Save a floor plan image in your project directory —
e.g., `my_apartment.png` in the project root. Your coding agent
needs read access to it, and the project directory is where it has
that.

**Step 2.** Prompt your agent:

> Use the image `my_apartment.png` as my floor plan and set it as
> the GUI background. Tell me what you did and when to refresh.

(Replace the filename with whatever you saved.)

**What you should see:**

The agent will move the image into the right place inside the
project, edit `.sandcastle/floorplan.json` to reference it, and
report back: where it placed the file, what it edited, and your
refresh cue (`http://localhost:8766`, **Ctrl+Shift+R** /
**Cmd+Shift+R**). After the refresh: the JS-drawn blueprint
disappears and your image takes its place. Devices render on top
in their (re-)laid-out positions.

---

## Iterate

Both paths produce a *draft* on the first pass. Vision-guided
alignment (Path B) and from-description sketching (Path A) are
roughly 80% right; the rest is a 2-minute iteration loop:

> The kitchen rectangle is too small — extend it east to where the
> island ends. Move the front door icon to the entryway, not the
> hallway.

The agent edits one or two fields, you refresh, you compare. Three
or four rounds is normal.

---

## What the agent kept the same

Behind the scenes, the agent **kept your room keys** (`bedroom`,
`kitchen`, etc.) even though their display labels and rectangles
changed. That's by design: those keys are what HA's area registry
and the agent's `list_devices` rely on. Renaming them would require
re-registering areas in HA and re-routing every device — a bigger
flow than this walkthrough covers.

So: your floor plan now looks like your home, but the simulator's
device list is unchanged. The agent still controls
`light.bedroom_main` whether your label says "Bedroom" or "Master
Bedroom."

If you want to *truly* restructure (different number of rooms,
totally different layout), you'd also need to edit
`.sandcastle/topology.json` and re-onboard areas in HA — that's a
developer-level customisation and out of scope here.

---

## Reset to the demo

Two ways:

```sh
# Option 1 — full reset; remove your custom floor plan, the seed default returns.
rm .sandcastle/floorplan.json

# Option 2 — keep your floor plan but lose the backdrop image overlay.
# Ask your agent: "remove the backdrop from .sandcastle/floorplan.json"
```

Either way, hard-refresh the browser to see the change.

---

## Where to go next

- **Add devices** to your new floor plan — see Task 3 of
  [`your-devices.md`](your-devices.md). The `floorplan auto` placer
  puts them in your new room rectangles automatically.
- **Plug in real devices.** Now that the floor plan looks like your
  home, [`adding-matter.md`](adding-matter.md) wires real Matter
  hardware onto it.
- **Add a curated scene.** A "movie night" that dims the right
  lights for *your* layout, not the demo's.

---

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Backdrop image doesn't render | Filename mismatch in `floorplan.json`'s `backdrop` field, or file isn't in `.sandcastle/images/`. Hard-refresh after fixing. |
| Devices land outside their rooms after backdrop swap | Room rectangles reshaped but device coordinates still room-local relative to the *old* shape. Run `sandcastle-sim floorplan auto --force` to re-place everything. |
| Devices in the wrong rooms | A room *key* changed (it shouldn't have). Either ask the agent to revert keys to the originals (and only update display `name`), or accept that you've stepped into the rename-a-room territory we don't cover in v1. |
| GUI is blank (no walls, no image) | `backdrop` references a file that doesn't exist on disk. Check `.sandcastle/images/<filename>` exists and matches the `backdrop` field. |
| `viewBox` too small / huge / off-centre | Image dimensions don't match the JSON's `viewbox`. Ask the agent to re-read the image and update `viewbox` to `[0, 0, width, height]`. |

---

## See also

- [`your-devices.md`](your-devices.md) — adding and moving devices.
- [`floorplan.md`](floorplan.md) — schema reference, vocabulary,
  persistence model.
- [`AGENTS.md`](../AGENTS.md) — the agent's runbook for both flows.
