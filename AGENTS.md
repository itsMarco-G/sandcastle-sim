# AGENTS.md — orientation for coding assistants

This file is for AI coding assistants (Claude Code, Cursor, Copilot,
Aider, etc.) and humans starting fresh on **Sandcastle Sim**.
Read it before making changes so your edits land in the right place
and follow this repo's conventions.

## What this repo is

`sandcastle-sim` — a pip-installable smart-home sandbox for AI
agents. Six processes work together:

| Process | Purpose | Where |
| --- | --- | --- |
| Mosquitto | MQTT broker | Docker, port 1883 |
| Home Assistant | entity registry + service registry | Docker, port 8123 |
| Smart-home MCP server | discovery + control + scenes + events (poll + SSE push) | `sandcastle_sim.mcp_server`, port 8765 |
| Device simulator | publishes simulated devices via MQTT discovery + serves the floor-plan GUI | `sandcastle_sim.simulator`, port 8766 |
| Built-in CLI agent | minimal Ollama + MCP one-shot agent (the dev-friendly entry point) | `sandcastle_sim.agent`, runs on demand |
| Ollama (optional) | hosts the local LLM the built-in agent talks to | port 11434 |

The MCP server is the canonical agent-facing surface. Anything that
speaks MCP streamable-HTTP works against
`http://localhost:8765/mcp/` — including the built-in CLI agent,
external clients, Claude Desktop, your own dev scripts. The
[`home_agent_perf`](https://github.com/itsMarco-G/home_agent_perf)
sibling repo is the reference fuller-agent integration.

## Repo map

```
sandcastle-sim/
├── AGENTS.md                            ← you are here
├── README.md                            ← user-facing entry point
├── pyproject.toml                       ← single top-level package
├── Makefile                             ← convenience targets (delegate to CLI)
├── docker-compose.yml                   ← Mosquitto + HA
├── docs/
│   ├── architecture.md                  ← system design (read second)
│   ├── tool-contract.md                 ← canonical MCP tool surface (v0.2)
│   ├── integrating-your-agent.md        ← code samples for any MCP client
│   ├── extending-the-simulator.md       ← add devices / scenes / tools
│   └── adding-matter.md                 ← swap in real Matter hardware
├── ha-config/
│   ├── configuration.yaml               ← HA bootstrap config (committed)
│   └── scenes.yaml                      ← curated demo scenes (committed)
├── mosquitto/config/mosquitto.conf
├── scripts/
│   ├── bootstrap_ha.py                  ← idempotent HA onboarding
│   ├── normalize_entity_ids.py          ← defensive entity-id cleanup
│   ├── smoketest_mcp.py                 ← MCP smoke test
│   ├── test_light_control.py            ← acceptance: light round-trip
│   └── test_milestone7.py               ← acceptance: control + behaviours
└── src/sandcastle_sim/
    ├── __init__.py
    ├── cli.py                           ← argparse entry: `sandcastle-sim`
    ├── agent/
    │   ├── __init__.py
    │   └── one_shot.py                  ← minimal Ollama + MCP loop
    ├── simulator/
    │   ├── topology.py                  ← single source of truth: which devices exist
    │   ├── behaviors.py                 ← motion / temp drift / vacuum / power
    │   ├── control.py                   ← aiohttp: GUI + /api/demo/trigger
    │   ├── lights.py / switches.py / locks.py / covers.py /
    │   │   climate.py / sensors.py / vacuum.py / media.py
    │   └── main.py                      ← simulator entry point
    ├── mcp_server/
    │   ├── server.py                    ← FastMCP: tools + /events SSE
    │   ├── ha_client.py                 ← async HA WebSocket client
    │   └── events.py                    ← significant-event classifier + buffer
    └── data/
        ├── gui/index.html               ← single-file floor plan
        └── seeds/                       ← config seeds for pip-installed users
```

Everything Python lives under `src/sandcastle_sim/` and is one
package. There are no per-subpackage pyprojects.

## Conventions to keep

These rules keep the codebase legible. Break them only with a
clearly stated reason.

1. **One concern per module.** `lights.py` doesn't know about MQTT
   topic structure (that's in `base.py`). `server.py` doesn't know
   how to subscribe to HA WebSocket (that's in `ha_client.py`).
2. **`docs/tool-contract.md` is the source of truth** for the
   agent's tool surface. If you change a tool's signature or
   return shape, update the contract first, the implementation
   second. Bump the version when the change is breaking.
3. **Comments explain WHY, not WHAT.** The code shows the what.
4. **No silent failures.** Every tool's error path returns a
   JSON dict with an `"error"` field. Tools never raise to the
   agent; the agent loop expects to handle errors as data.
5. **Lazy retry over blocking startup.** Backends that aren't
   running at boot shouldn't crash the simulator/MCP server. They
   retry on the next caller's request (3 s cooldown is the
   standard cadence).
6. **No emojis in code, comments, or commit messages.** They
   render inconsistently across terminals and editors.
7. **Tests stay fast and offline where possible.** Anything in
   `scripts/test_*.py` runs against the live stack and acts as
   integration tests; unit-test territory should not require
   Mosquitto / HA / Ollama.
8. **The CLI is a thin wrapper.** `cli.py` shells out to module
   entry points and to `docker compose`. Don't put feature logic
   there — it belongs in the relevant sub-package.

## Adding common things

### Adding a new device type to the simulator

1. New file `src/sandcastle_sim/simulator/<type>.py` with a class
   subclassing `Device` from `base.py`. Implement
   `discovery_extras()` and `handle_command()`.
2. Add a list of specs to `topology.py` and an entry in
   `ALL_BY_DOMAIN`.
3. Wire it in `main.py:_build_devices()`.
4. (Optional) Add a renderer to `data/gui/index.html`'s
   `updateDevice` dispatcher and an entry in the `DEVICES` map for
   room placement.

### Adding a new MCP tool

1. New `@mcp.tool()` function in
   `src/sandcastle_sim/mcp_server/server.py`. Add validation,
   return resulting state on success, return `{"error": "..."}` on
   failure.
2. Document the tool in `docs/tool-contract.md`. Bump the version.
3. (Optional) Add a voice-mode announcement phrase in the consuming
   agent (e.g. `home_agent_perf/app/tool_loop.py:tool_announcement`).

### Adding a new significant-event kind

1. Extend `classify()` in
   `src/sandcastle_sim/mcp_server/events.py`.
2. Document the new kind in `docs/tool-contract.md` §3.

### Adding a new curated scene

1. New entry in `ha-config/scenes.yaml`.
2. Restart HA (`make restart` or `sandcastle-sim down && sandcastle-sim up`).
   The MCP server picks it up automatically.

## Things to watch for

- **Don't call back into the HA WebSocket from inside an event
  handler.** The reader loop is single-threaded; awaiting a fresh
  WS request inside the handler deadlocks. See the comment block
  on `_on_ha_event` in `mcp_server/server.py`. Resolve registry
  data lazily at tool-call time instead.
- **Entity IDs are sticky in HA's registry** once assigned. If you
  rename a `device.name` after first registration, the entity_id
  doesn't change automatically. `scripts/normalize_entity_ids.py`
  is the defensive cleanup; in normal flows the topology's `name`
  values are chosen so `slugify(name)` matches the contract slug.
- **`scenes.yaml` is committed** (curated demo content).
  `automations.yaml` and friends are gitignored (HA runtime).
- **Per-session vs per-process for FastMCP lifespan**: the
  streamable-HTTP transport calls lifespan per session. Don't close
  long-lived resources at session end (we explicitly keep the HA
  WS open across sessions).
- **Pip-installed vs editable.** The CLI picks a workdir based on
  whether `cwd/docker-compose.yml` exists. Editable users keep
  using the repo root; pip-installed users get
  `~/.local/share/sandcastle-sim` materialised from
  `data/seeds/`.

## Running the stack

The headline command bundles everything:

```bash
sandcastle-sim start             # docker stack + bootstrap + sim + MCP, all in one
# Open http://localhost:8766
sandcastle-sim "your prompt"     # one-shot Ollama+MCP agent
sandcastle-sim stop              # gracefully tears down everything
sandcastle-sim status            # port reachability + background PIDs
sandcastle-sim logs sim          # tail simulator log; also: mcp / ha / mosquitto / all
```

For component-level control during development:

```bash
sandcastle-sim up                # Mosquitto + HA only
sandcastle-sim bootstrap         # idempotent HA onboarding
sandcastle-sim mcp               # MCP server (foreground)
sandcastle-sim sim               # simulator + GUI (foreground)
sandcastle-sim down              # docker compose down only
```

GUI: `http://localhost:8766`. MCP: `http://localhost:8765/mcp/`.
SSE event push: `http://localhost:8765/events`.

Background processes (sim, MCP) drop PID files and log files
under `<workdir>/.sandcastle/`. `start` and `stop` use these to
detect already-running components and to deliver SIGTERM. The
runtime helpers live in `src/sandcastle_sim/runtime.py`.

`sandcastle-sim --help` lists every subcommand. `make help` does
the same thing for the legacy convenience targets.

## Verifying changes with the eval suite

This repo ships a regression-net eval harness — use it as your
guardrail before reporting a task as done. The workflow:

```bash
# 1. Capture the current behavior as the baseline.
sandcastle-sim eval --save-baseline

# 2. Make your changes.
# ... edit code, edit prompts, change the topology, etc. ...

# 3. Re-run and diff.
sandcastle-sim eval --diff
```

Step 3 prints a structured diff highlighting:

- **REGRESSIONS** — cases that were passing and now fail
- **LATENCY REGRESSIONS** — still passing but >20% AND >1s slower
- **PROGRESSIONS** — cases that were failing and now pass
- **NEW CASES** — added to the suite since the baseline
- **REMOVED CASES** — in the baseline but not the current suite

Exit code is non-zero on any regression, so a coding-agent loop
can detect "my change broke something" without parsing prose. The
`failures:` field on each regression names the specific
expectation that didn't hold (which tool wasn't called, which
final-state attribute didn't match, etc.).

Constraints:

- Requires a live stack: `sandcastle-sim start` + `ollama serve`
  with the configured model pulled.
- Each case adds ~3-10s to the run, so a 5-case suite takes
  ~30s warm. Don't burn this budget on every micro-iteration —
  use it as a checkpoint, not a tight loop.
- The baseline lives at `<workdir>/.sandcastle/eval-baseline.json`
  (already gitignored). Don't commit it.

Custom eval suites: write your own `evals/my-suite.yaml` and
point at it with `--suite path/to/file.yaml`. See
`evals/quick.yaml` for the schema.

## When in doubt

- Architecture questions → `docs/architecture.md`
- Tool surface / signatures → `docs/tool-contract.md`
- Integrating an agent → `docs/integrating-your-agent.md`
- Adding things → `docs/extending-the-simulator.md`
- Pi notes → `README.md` § Hardware
