# Hardware

Sandcastle Sim runs anywhere with Docker and Python 3.10+. Notes per platform.

## Resource budget

The Sandcastle stack alone is small:

- Mosquitto + HA (Docker): ~600 MB RAM
- Simulator + MCP server (Python): ~150 MB RAM
- **Total without LLM**: ~750 MB

If you run the model locally with Ollama:

- gemma4:e4b: ~3.5 GB on top
- qwen2.5:7b-instruct: ~4.7 GB
- Smaller models (`gemma3:2b`, `qwen2.5:3b`): ~1.5–2 GB

Cloud agents (Claude API, OpenAI, anything that speaks MCP) sidestep the LLM cost entirely.

## Dev machines

### Linux
Native Docker, native Python — fastest path. Anything from the last decade works.

### macOS (Intel or Apple Silicon)
Docker Desktop runs HA + MQTT. Native Python runs the simulator + MCP server. Apple Silicon is markedly faster for local LLM inference via Metal.

### Windows
WSL2 with Docker Desktop's WSL integration.

## Raspberry Pi 5 (8 GB)

Runs the full stack natively. Tested on Raspberry Pi OS Bookworm 64-bit.

- Docker via `docker.io` from apt or Docker Desktop
- Optional Ollama with a 4B model
- **Total**: ~5 GB of 8 GB, leaving headroom

### Headless setup
The floor-plan GUI is in your browser, not on the Pi. Replace `localhost` with the Pi's IP everywhere.

### SSD / USB boot recommended
The HA + MQTT containers do ~50 MB/h of writes. microSD works but expect to replace the card every couple of years on 24/7 operation.

## Raspberry Pi 4 (8 GB)

Workable. Use a smaller LLM if running locally — Pi 4's CPU is the bottleneck for token throughput:

```bash
ollama pull gemma3:2b
export SANDCASTLE_MODEL=gemma3:2b
```

Cloud agents sidestep this entirely.

## Raspberry Pi 4 (4 GB)

Tight but usable for non-local agents. Skip local LLMs; point a cloud agent at the kit instead.

## Picking the model

The CLI agent uses `gemma4:e4b` by default. Override per-call or via env var:

```bash
sandcastle-sim --model qwen2.5:7b-instruct "turn off the kitchen light"
# or
export SANDCASTLE_MODEL=qwen2.5:7b-instruct
```

Ollama on Pi runs natively (ARM64). Install with the standard `curl` script and pull a small model first before reaching for anything 4B+.
