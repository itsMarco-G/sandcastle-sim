# sandcastle-sim

[![build](https://img.shields.io/github/actions/workflow/status/itsMarco-G/sandcastle-sim/pr-check.yml?branch=main)](https://github.com/itsMarco-G/sandcastle-sim/actions)
[![pypi](https://img.shields.io/pypi/v/sandcastle-sim)](https://pypi.org/project/sandcastle-sim/)
[![license](https://img.shields.io/pypi/l/sandcastle-sim)](https://github.com/itsMarco-G/sandcastle-sim/blob/main/LICENSE)
[![python](https://img.shields.io/pypi/pyversions/sandcastle-sim)](https://pypi.org/project/sandcastle-sim/)

> **Developer preview.** Active early release. Feedback and issues welcome.

Sandcastle Sim is a sandbox for smart-home AI agents. Real [Home Assistant](https://github.com/home-assistant/core) (HA) and [Mosquitto](https://mosquitto.org/) run in Docker; devices are simulated and publish via standard MQTT discovery, so HA can't tell them apart from real hardware. An agent that works here works against a real home unchanged. HA is the open-source hub most DIY smart homes are built on, and it's what your agent will be talking to in production.

This is for developers building smart-home agents, whether you're coming from the LLM/agent side and want a realistic target, or you already run Home Assistant and want to start layering agents onto your setup. One command brings up the full stack. A built-in CLI bridges your prompts to a local model via [Ollama](https://ollama.com), so you can get to a working demo in minutes and then drop in your own agent when you're ready to iterate.

![Architecture diagram showing Home Assistant, Mosquitto MQTT broker, and the device simulator running together in Docker, with an agent connecting via the MCP server.](docs/architecture.svg)

## Install

### Prerequisites

- [Docker](https://docs.docker.com/compose/) with Compose v2
- [Ollama](https://ollama.com) for the built-in CLI. Optional if you're connecting your own MCP agent.
- Python >= 3.10

### Setup

Create and activate a virtual environment so the install stays isolated from your system Python:

```
python -m venv .venv
source .venv/bin/activate
```

Then install:

```
pip install sandcastle-sim
```

Optional: if you're building from source or making active changes, install editable from a checkout with `pip install -e .` to pick up your edits and any unreleased changes on `main`.

### Second terminal: set up Ollama

Install Ollama if you haven't already, do this on a second terminal as it takes a few minutes. See [ollama.com/download](https://ollama.com/download) for installation. Then pull the model and start Ollama.

```
ollama pull gemma4:e4b
```

```
ollama serve
```

Proceed with the steps below while the model is downloading and Ollama is starting up.

### Bring up the simulator stack

In your original terminal, run:

```
sandcastle-sim start
```

When the castle banner prints in the terminal, the stack is up. Open `http://localhost:8766` and you should see the floor plan. Click any device to turn it on or off, dim a light, or open a blind. That's the simulated home 🏠💡

## Chat — `sandcastle-sim chat`

> **Note:** Only start chat once Ollama is up and the model is ready from the earlier steps.

```
sandcastle-sim chat
```

A chat panel shows up listing the model and the available tools. For your first prompt, try:

```
set up welcome guest
```

![set up welcome guest](docs/welcome-guest.png)

You should see the floorplan update from the model's tool call like above.

Congratulations! You're now ready to explore and control your simulated smarthome with your agent 🏰

Run `sandcastle-sim --help` for the full command list.

Using an AI coding agent (Claude Code, Codex, Copilot)? Read [AGENTS.md](AGENTS.md) first.

## Evals — `sandcastle-sim eval`

AI agents aren't deterministic. The same prompt can produce different outputs as you change the model, the system prompt, or the tool config. Small changes break things in non-obvious ways. The evals are how you catch that, and a quick way to see how performance looks on your hardware.

### Baseline (per host)

End-to-end latency on the bundled `quick.yaml` suite against the live stack (HA + MQTT + simulator + MCP) with `gemma4:e4b` (~4 B params, q4_K_M).

**Avg/case** is the full round trip for one prompt: the model reads it, decides which tool to call, the MCP server dispatches the call, Home Assistant executes it and updates state, and the model writes its reply back. The eval pre-warms the model with a single token so per-case timings reflect steady-state cost only. The cold model-load you see once at the start of `sandcastle-sim chat` is excluded.

| Host | Pass | Avg/case | Slowest |
| --- | --- | --- | --- |
| **DGX Spark** (NVIDIA GB10, 128 GB unified) | 5/5 | 3.7 s | `state_query` 9.0 s |
| **MacBook Pro M3 Max** (36 GB unified) | 5/5 | 3.7 s | `state_query` 6.9 s |
| **MacBook Pro M3 Pro** (18 GB unified) | 5/5 | 6.3 s | `state_query` 15.7 s |

Numbers are **median of 3 repeats per case** (`--repeat 3`, the default) so single-shot noise on bandwidth-bound laptops doesn't show up as performance changes.

The five cases in `quick.yaml`:

- `light_off`: "turn off the kitchen counter light"
- `scene_named`: "set up movie night"
- `lock_door`: "lock the front door"
- `climate_setpoint`: "set the temperature to 22"
- `state_query`: "what lights are on right now?" (the heaviest case, since the agent has to list devices, then answer)

### Try it

**1. Save a baseline snapshot** of how the agent behaves right now:

```
sandcastle-sim eval --save-baseline
```

**2. First go: see the diff workflow without writing any code.** Toggle off the agent's tool-routing optimisation for one run:

```
sandcastle-sim eval --no-routing --diff
```

Every case lands a bit slower (no failures), and the diff surfaces clean latency regressions against the baseline you just saved. The flag scopes to that one command; the next eval reverts to defaults automatically.

**3. Normal use, after you change your agent.** Make any change, then:

```
sandcastle-sim eval --diff
```

The report leads with cases that used to pass and now fail. Cases that got noticeably slower show up too. Exit code is non-zero if anything regressed, so a coding agent running this in a loop can tell when its own changes broke something.

[evals/quick.yaml](evals/quick.yaml) is the starter suite. Write your own to match your agent's acceptance bar.

When you're done, `sandcastle-sim stop` brings the simulator down cleanly.

## Next steps

Short coding-agent-driven walkthroughs make the sandbox yours. Paste each prompt into Claude Code / Cursor / Codex / Copilot in the project root.

- [docs/your-devices.md](docs/your-devices.md): tour the stack, move devices, add new ones
- [docs/your-floorplan.md](docs/your-floorplan.md): replace the blueprint with your real home (image or agent-sketched)
- [docs/integrating-your-agent.md](docs/integrating-your-agent.md): connect any MCP-speaking agent (the most common next step for agent developers)
- Connect cloud models: architecturally supported, not yet wired up in this preview

## Read more

- [AGENTS.md](AGENTS.md): orientation for AI coding agents
- [docs/architecture.md](docs/architecture.md): what runs where and why
- [docs/tool-contract.md](docs/tool-contract.md): full MCP tool surface
- [docs/integrating-your-agent.md](docs/integrating-your-agent.md): connect any MCP-speaking agent
- [docs/your-devices.md](docs/your-devices.md): customise the devices in your home
- [docs/your-floorplan.md](docs/your-floorplan.md): customise the floor plan with a sketch or image
- [docs/floorplan.md](docs/floorplan.md): floor-plan schema, vocabulary, persistence reference
- [docs/extending-the-simulator.md](docs/extending-the-simulator.md): add new device classes, MCP tools, scenes
- [docs/hardware.md](docs/hardware.md): Mac, Linux, Pi 4/5 sizing notes
- [docs/adding-matter.md](docs/adding-matter.md): swap in real Matter hardware
- [CONTRIBUTING.md](CONTRIBUTING.md): how to contribute

## License
This project is licensed under the Apache 2.0 License. See the [LICENSE](LICENSE) file for details.