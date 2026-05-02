# sandcastle-sim

![build](https://img.shields.io/github/actions/workflow/status/itsMarco-G/sandcastle-sim/pr-check.yml?branch=main)
![pypi](https://img.shields.io/pypi/v/sandcastle-sim)
![license](https://img.shields.io/pypi/l/sandcastle-sim)
![python](https://img.shields.io/pypi/pyversions/sandcastle-sim)

Sandcastle Sim is a sandbox for smart-home AI agents. Real Home Assistant (HA) and Mosquitto run in Docker; the devices are simulated and publish via standard MQTT discovery. From HA's perspective there's no difference between a simulated bulb and a real one, so an agent that works here works against a real home unchanged.

For developers building smart-home agents. One command brings up the full stack. A built-in CLI agent gets you to a working demo in minutes, then drop in your own when you're ready to iterate on prompts, UX, and edge cases.


![architecture](docs/architecture.svg)

## Contents

- [Install](#install)
- [Quickstart](#quickstart)
- [Eval suite](#eval-suite)
- [Connect your agent](#connect-your-agent)
- [Customize](#customize)
- [Read more](#read-more)

## Install

### Prerequisites

- [Docker](https://docs.docker.com/compose/) with Compose v2
- Python >= 3.10
- [Ollama](https://ollama.com) for the built-in CLI agent. Optional if you're connecting your own MCP agent.
- Tested on Mac (Apple Silicon), Linux, and Raspberry Pi 4/5. Windows not yet tested.

### Setup

Create and activate a virtual environment so the install stays isolated from your system Python:

```sh
python -m venv .venv
source .venv/bin/activate
```

Then install:

```sh
pip install sandcastle-sim
```

Planning to make code changes? Install editable from a checkout instead (`pip install -e .`). See [CONTRIBUTING.md](CONTRIBUTING.md) for the full dev setup.

## Quickstart

### Start the stack

```sh
sandcastle-sim start
```

When the castle banner prints in the terminal, the stack is up. Open `http://localhost:8766` and you should see the floor plan with every device laid out across six rooms. Click any device to flip it on or off, dim a light, or open a blind. That's the simulated home.

### Drive it with natural language

In a separate terminal, pull the model and start Ollama:

```sh
ollama pull gemma4:e4b
```

```sh
ollama serve
```

Then back in your first terminal:

```sh
sandcastle-sim chat
```

A chat panel shows up listing the model and the available tools. Try prompts like:

```
turn off the kitchen counter light
set up movie night
what just happened in the home?
```

Each response prints the tool calls the agent fired. The floor plan reflects every change in real time, so you can watch the kitchen light go dark or the bedroom lamp shift colour as the prompts run.

Run `sandcastle-sim --help` for the full command list.

Using an AI coding agent (Claude Code, Codex, Copilot)? Read [AGENTS.md](AGENTS.md) first.

## Eval suite

AI agents aren't deterministic. The same prompt can produce different outputs as you change the model, the system prompt, or the tool config. Small changes break things in non-obvious ways. The eval suite is how you catch that.

Save a snapshot of how the agent behaves right now:

```sh
sandcastle-sim eval --save-baseline
```

Make any change to your agent, then check what shifted:

```sh
sandcastle-sim eval --diff
```

The report leads with cases that used to pass and now fail. Cases that got noticeably slower show up too. Exit code is non-zero if anything regressed, so a coding agent running this in a loop can tell when its own changes broke something.

To see the diff workflow in action without writing any code, toggle off the agent's tool-routing optimisation for one run:

```sh
sandcastle-sim eval --no-routing --diff
```

Every case lands a bit slower (no failures), and the diff surfaces clean latency regressions. The flag scopes to that one command; the next eval reverts to defaults automatically.

[evals/quick.yaml](evals/quick.yaml) is the starter suite. Write your own to match your agent's acceptance bar.

## Connect your agent

Point any MCP client at `http://localhost:8765/mcp/`:

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client("http://localhost:8765/mcp/") as (r, w, _):
    async with ClientSession(r, w) as session:
        await session.initialize()
        tools = await session.list_tools()
```

[docs/integrating-your-agent.md](docs/integrating-your-agent.md) covers the Anthropic SDK, OpenAI SDK, and raw streamable-HTTP samples.

## Customize

The kit is set up to be forkable. Topology defines what's in the home, the simulator brings devices to life, the MCP server exposes the contract your agent calls. Here's where each one lives.

- `src/sandcastle_sim/simulator/topology.py` : the home's device list, areas, friendly names
- `src/sandcastle_sim/simulator/` : one module per device type (lights, locks, climate, sensors, vacuum, covers)
- `src/sandcastle_sim/mcp_server/server.py` : MCP tool surface and HA integration
- `src/sandcastle_sim/agent/` : built-in Ollama + MCP agent (one-shot and chat REPL)
- `src/sandcastle_sim/data/gui/index.html` : floor-plan GUI
- `evals/quick.yaml` : starter eval suite for the regression net
- `pyproject.toml` : runtime and dev deps; optional extras `.[dev]`

[docs/extending-the-simulator.md](docs/extending-the-simulator.md) for the deep dive.

## Read more

- [AGENTS.md](AGENTS.md) : orientation for AI coding agents
- [docs/architecture.md](docs/architecture.md) : what runs where and why
- [docs/tool-contract.md](docs/tool-contract.md) : full MCP tool surface
- [docs/integrating-your-agent.md](docs/integrating-your-agent.md) : connect any MCP-speaking agent
- [docs/extending-the-simulator.md](docs/extending-the-simulator.md) : add devices, tools, scenes
- [docs/hardware.md](docs/hardware.md) : Mac, Linux, Pi 4/5 sizing notes
- [docs/adding-matter.md](docs/adding-matter.md) : swap in real Matter hardware
- [CONTRIBUTING.md](CONTRIBUTING.md) : how to contribute

---

Apache-2.0
