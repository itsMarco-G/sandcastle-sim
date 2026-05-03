"""Sandcastle Sim — a smart-home sandbox for AI agents.

A self-contained kit you can point any MCP-speaking agent at:
Home Assistant + MQTT + a fully simulated home + MCP server + SSE
event push + a live floor-plan GUI for visual feedback.

Three sub-packages:

* ``sandcastle_sim.simulator`` — MQTT device simulator with
  realistic behaviours (motion, temperature drift, vacuum movement,
  power summing) and the floor-plan GUI host.
* ``sandcastle_sim.mcp_server`` — FastMCP server exposing tools for
  discovery, control, scenes, and events, backed by a Home Assistant
  WebSocket client.
* ``sandcastle_sim.agent`` — minimal one-shot CLI agent (Ollama +
  MCP) for trying the kit without writing any agent code.

The dev-facing API is the CLI: see ``sandcastle_sim.cli`` and
``sandcastle-sim --help``. For programmatic use, the sub-package
modules are publicly importable.
"""

__version__ = "0.1.1"
