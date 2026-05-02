"""Minimal one-shot agent for the Sandcastle Sim CLI.

This is a deliberately tiny reference agent. It has just enough
machinery to run a single user prompt through an Ollama-served LLM
that can call the smart-home MCP tools. No memory, no voice, no
multi-turn history — those live in fuller agents like
``home_agent_perf``. The point of this module is:

* Let new users verify the kit works end-to-end with one command:

    sandcastle-sim "turn off the kitchen light"

* Be a readable reference for "how do I wire MCP to my LLM?" —
  ~200 lines covering Ollama's native ``/api/chat`` endpoint,
  MCP tool discovery, the tool-call loop, and result printing.

Anything more sophisticated (planning, scenes-aware prompts,
parallel dispatch, voice announcements, push notifications) lives
in the consumer agent. This module stays simple on purpose.
"""

from .chat import run_chat_sync
from .one_shot import OneShotAgent, run_one_shot

__all__ = ["OneShotAgent", "run_chat_sync", "run_one_shot"]
