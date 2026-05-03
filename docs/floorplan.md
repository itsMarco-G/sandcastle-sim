# Floor-plan customisation

The GUI's floor plan (rooms, device positions, optional backdrop
image) lives as data in JSON. There are two locations:

- **Bundled seed:** `src/sandcastle_sim/data/seeds/floorplan.json`.
  Read-only from the user's perspective; updated by `git pull` and
  `pip install --upgrade`. This is the demo six-room apartment.
- **Your home:** `<workdir>/.sandcastle/floorplan.json`. Editable;
  survives package updates. Seeded from the bundled default on first
  `sandcastle-sim start` and never overwritten afterwards.

The renderer is still JS in `index.html`; only the *layout* moved
to config. That makes the file editable by hand, by a coding agent,
or by a built-in command — without anyone touching the GUI source.

This doc covers:

1. The two-phase workflow (auto-layout first, then natural-language
   corrections).
2. The schema.
3. Vocabulary a coding agent can use to translate user intent into
   coordinate edits.

For *adding* a new device type to the simulator (a fan, a humidifier,
etc.), see [`extending-the-simulator.md`](extending-the-simulator.md).
This doc is about *placement*, not new device classes.

---

## 1. Two-phase workflow

### Phase 1 — auto-layout (deterministic)

```bash
sandcastle-sim floorplan auto            # places only new devices
sandcastle-sim floorplan auto --force    # re-lays out everything
```

Reads the live device inventory from the MCP server (so the stack
must be up — `sandcastle-sim status` to check), maps each entity to a
floor-plan type (`light`, `motion`, `contact`, …), and places it by
deterministic priors:

| Type      | Default placement                                    |
| --------- | ---------------------------------------------------- |
| `light`   | spread along the upper third of the room             |
| `temp`    | low-left interior wall                               |
| `motion`  | mid-room, spread horizontally                        |
| `contact` | along the bottom edge (typically a door / window)    |
| `leak`    | low-right corner                                     |
| `smoke`   | top edge (ceiling-ish), spread horizontally          |
| `lock`    | mid-height, near the entry wall                      |
| `cover`   | top edge of the room (windows are usually high)      |
| `switch`  | mid-height, near a side wall                         |
| `climate` / `power` / `vacuum` | not placed; render in info bar      |

These are crude on purpose. The goal is "good enough first draft"
that the user can correct, not interior-design accuracy.

### Phase 2 — natural-language corrections

Open the GUI at `http://localhost:8766`, scroll through, then ask a
coding agent to nudge specific devices. The agent reads
`floorplan.json`, edits one or a few lines, you refresh the browser.

```
"move the kitchen counter light to the south wall"
"swap the two bedroom temperature sensors"
"move the front-door contact a bit left"
"center the living-room motion sensor"
```

Convergence usually takes 2–4 rounds. Refresh the browser to see
each change.

---

## 2. Schema

```jsonc
{
  "version": 1,
  "viewbox": [0, 0, 1100, 720],     // SVG canvas — match if you change room layout
  "rooms": {
    "kitchen": {
      "x": 0, "y": 330,             // top-left in canvas coords
      "w": 350, "h": 270,           // size
      "name": "Kitchen"             // label shown on the floor plan
    },
    // ... one entry per room
  },
  "devices": {
    "light.kitchen_counter": {
      "area": "kitchen",            // must match a key in `rooms`
      "type": "light",              // see the table above
      "x": 100, "y": 80             // ROOM-LOCAL pixels (not canvas)
    },
    "climate.home_thermostat": {
      "area": null, "type": "climate"   // whole-home — no x/y
    }
    // ... one entry per device
  }
}
```

Validation happens server-side every time the GUI fetches
`/api/floorplan`. If the file is malformed, the GUI shows an error
banner instead of crashing — readable, recoverable, no half-rendered
state. The validator enforces:

- every device's `area` exists in `rooms` (or is `null`)
- every room device has numeric `x` and `y`
- `(x, y)` lies inside the device's room rect
- rooms have positive `w` / `h`

You can test the file directly:

```bash
.venv/bin/python -c "
from sandcastle_sim.floorplan import load_floorplan, resolve_floorplan_path
data = load_floorplan(resolve_floorplan_path())
print('OK —', len(data['devices']), 'devices')
"
```

---

## 3. Coding-agent vocabulary

If you're a coding agent reading this — when a user describes a
move in natural language, translate to a coordinate edit in
`floorplan.json` using the conventions below. Keep edits small (one
or a few lines per turn). Validate the file afterwards by re-loading
it.

Coordinates are **room-local**: x = 0 is the room's left edge,
y = 0 is the room's top edge, x = `room.w` is the right edge,
y = `room.h` is the bottom edge.

| User says (intent)                        | Translation                                         |
| ----------------------------------------- | --------------------------------------------------- |
| "move X to the north / top wall"          | y = `MARGIN` (≈ 30); keep current x                 |
| "move X to the south / bottom wall"       | y = `room.h - MARGIN`; keep current x               |
| "move X to the west / left wall"          | x = `MARGIN`; keep current y                        |
| "move X to the east / right wall"         | x = `room.w - MARGIN`; keep current y               |
| "move X to the corner" (NE/NW/SE/SW)      | combine the above                                   |
| "center X" / "center X in the room"       | x = `room.w / 2`, y = `room.h / 2`                  |
| "move X next to Y"                        | x = `Y.x + 80`, y = `Y.y` (or - 80 if no room)      |
| "swap X and Y"                            | exchange the `x` and `y` of the two entries        |
| "move X a bit left / right / up / down"   | offset by ~5% of `room.w` / `room.h` in that axis  |
| "spread out the sensors in [room]"        | re-run auto-layout for that room only (manual edit) |
| "move X to room R"                        | change `area` to R, re-run layout for X (or pick a sensible (x, y) inside R's rect) |

Practical rules:

- **Snap to multiples of 10.** Coordinates round nicely; diffs are
  more legible (`x: 280` reads better than `x: 283.7`).
- **Keep at least `MARGIN = 30` from every edge** — devices flush
  against a wall look glitchy.
- **Don't bulk-rewrite the file.** One user instruction → one or two
  field edits. If the user asks for a wholesale redo, run
  `sandcastle-sim floorplan auto --force`.
- **Always verify after writing.** Reload `floorplan.json` and check
  it parses; the schema validator catches out-of-bounds positions.

---

## 4. Persistence

| Location | Role | Persists across `git pull` / `pip upgrade` |
| --- | --- | --- |
| `data/seeds/floorplan.json` (in the package) | Bundled demo, read-only. | n/a — gets updated. |
| `<workdir>/.sandcastle/floorplan.json` | Your home. Edited by you, your agent, or `floorplan auto`. | ✓ — outside the package. |
| `<workdir>/.sandcastle/images/<file>` | Backdrop images you've dropped in. | ✓ |

The control server resolves which file to load on every request:
workdir copy if it exists, else the bundled seed. So:

- Fresh install, no customisation → workdir empty → bundled demo
  renders.
- After `sandcastle-sim start` → workdir is seeded from the demo;
  subsequent edits go to the workdir copy.
- Want to reset to demo? `rm <workdir>/.sandcastle/floorplan.json`
  and the next request falls back to the seed.

**The package is never written to** by any sandcastle command. The
demo is, by construction, indelible. Your home is, by construction,
durable.

(Same model applies to `topology.json` — see
[`your-home.md`](your-home.md) for adding device instances.)

## 5. Bring your own home (the backdrop image)

Today the bundled demo is a stylised six-rectangle apartment, drawn
in JS. If you'd like the GUI to show *your* home instead — a
real-estate listing PNG, a clean architect drawing, a hand sketch —
the schema supports a backdrop image:

```jsonc
{
  "viewbox": [0, 0, 1600, 1100],     // match your image's pixel size
  "backdrop": "my_home.png",
  "rooms":   { ...polygons aligned to the image... },
  "devices": { ...placed by floorplan auto or by hand... }
}
```

Where to put the image: `<workdir>/.sandcastle/images/my_home.png`.
The control server serves files in that directory at `/images/<name>`
so the JSON's `backdrop` field is just a filename — no path
traversal, no leaking the workdir layout to the browser.

When `backdrop` is set the renderer:

- Draws the image as the SVG floor.
- Skips the JS-drawn walls, room tints, doors, and FURNITURE.
- Honours the JSON's `viewbox` so the icons land at the right pixel
  coordinates over the image.

Device icons still render on top in their room-local positions, so
the same Phase-1/Phase-2 placement workflow applies. A coding agent
flow would be:

1. *"Use this image of my home as the floor plan."*
2. Agent edits `floorplan.json` with `backdrop: "my_home.png"`,
   updates `viewbox` to the image's dimensions, drafts room
   rectangles roughly aligned to the visible rooms.
3. *"Now place all my devices."* — `sandcastle-sim floorplan auto`.
4. Iterate with the Phase-2 vocabulary above.

---

## See also

- [`extending-the-simulator.md`](extending-the-simulator.md) — adding
  new device *types* (renderers, simulator classes).
- [`adding-matter.md`](adding-matter.md) — the matter onboarding flow,
  which now uses `floorplan auto` to place new real devices.
- [`AGENTS.md`](../AGENTS.md) — coding-agent orientation for the repo
  as a whole.
