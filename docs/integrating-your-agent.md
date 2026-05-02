# Integrating your agent

Sandcastle Sim is transport-agnostic about which agent connects to
it. Anything that speaks the
[Model Context Protocol](https://modelcontextprotocol.io)
streamable-HTTP transport works against `http://localhost:8765/mcp/`.
This doc has copy-pasteable starting points for the common paths.

The companion event stream at `http://localhost:8765/events` is plain
SSE — your agent can consume it for real-time push without needing
MCP-level subscription plumbing.

Two reference integrations exist:

- **The built-in CLI agent** at
  [`src/sandcastle_sim/agent/one_shot.py`](../src/sandcastle_sim/agent/one_shot.py)
  — ~150 lines, minimal, Ollama-only. Read it as a "hello world"
  for wiring MCP tool-discovery to a tool-using LLM.
- **The full reference**
  ([`home_agent_perf`](https://github.com/itsMarco-G/home_agent_perf))
  — production-grade agent with planning, voice mode, push-event
  consumption, dynamic tool routing, parallel dispatch, the lot.
  See `app/tools/mcp_client.py` (sync wrapper over async MCP) and
  `app/tools/smart_home.py` (registrar + SSE consumer).

---

## Path 1 — Python with the Anthropic SDK

The shortest path: Claude with native MCP support.

```python
# pip install anthropic mcp
import asyncio
from anthropic import AsyncAnthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://localhost:8765/mcp/"


async def main():
    async with streamablehttp_client(MCP_URL) as (read, write, _gs):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_list = await session.list_tools()

            # Convert MCP tool schemas to Anthropic tool-use format
            anthropic_tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema or {"type": "object"},
                }
                for t in tool_list.tools
            ]

            client = AsyncAnthropic()
            messages = [{"role": "user", "content": "Turn on the kitchen counter light."}]

            while True:
                response = await client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1024,
                    tools=anthropic_tools,
                    messages=messages,
                )
                if response.stop_reason == "end_turn":
                    print(response.content[0].text)
                    break
                if response.stop_reason == "tool_use":
                    messages.append({"role": "assistant", "content": response.content})
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            result = await session.call_tool(block.name, block.input)
                            text = result.content[0].text if result.content else ""
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": text,
                            })
                    messages.append({"role": "user", "content": tool_results})


asyncio.run(main())
```

Run it. You should see "The kitchen counter light is now on" in the
console and the light icon brighten on the floor plan.

---

## Path 2 — Python with OpenAI tool-use (or any OpenAI-compat endpoint)

Works with OpenAI directly, with Ollama via its OpenAI-compatible
API, with vLLM, with any inference server that supports the OpenAI
chat-completions tool-use shape.

```python
# pip install openai mcp
import asyncio, json
from openai import AsyncOpenAI
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    async with streamablehttp_client("http://localhost:8765/mcp/") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()

            # OpenAI tool format wraps the same schema in `function`
            openai_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": t.inputSchema or {"type": "object"},
                    },
                }
                for t in tools.tools
            ]

            # Point at OpenAI, Ollama (base_url=http://127.0.0.1:11434/v1), or vLLM
            client = AsyncOpenAI()
            messages = [{"role": "user", "content": "Set up movie night."}]

            for _ in range(5):  # iteration ceiling
                resp = await client.chat.completions.create(
                    model="gpt-4o-mini",
                    tools=openai_tools,
                    messages=messages,
                )
                msg = resp.choices[0].message
                messages.append(msg.model_dump(exclude_none=True))

                if not msg.tool_calls:
                    print(msg.content)
                    break

                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    result = await session.call_tool(tc.function.name, args)
                    text = result.content[0].text if result.content else ""
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": text,
                    })


asyncio.run(main())
```

---

## Path 3 — Raw streamable-HTTP (any language)

If you're building from another language or want to skip the SDK,
the MCP transport is plain HTTP + JSON-RPC 2.0. POST your initialise
request, then your tool calls, to `/mcp`. Each request expects the
session ID returned by the first response in the
`Mcp-Session-Id` header.

The MCP spec is the source of truth:
<https://spec.modelcontextprotocol.io>. The streamable-HTTP transport
is one of the documented transports.

For Node / Bun, the official TypeScript SDK
([`@modelcontextprotocol/sdk`](https://www.npmjs.com/package/@modelcontextprotocol/sdk))
mirrors the Python one. For Go, see
[`github.com/mark3labs/mcp-go`](https://github.com/mark3labs/mcp-go).

---

## Subscribing to the event stream

Significant home transitions (door open / leak / smoke / lock change /
vacuum state) are pushed via SSE on a sibling endpoint. Plain HTTP
streaming, no MCP-protocol involvement:

```python
import httpx

with httpx.stream("GET", "http://localhost:8765/events") as r:
    event_name = ""
    data_lines = []
    for raw in r.iter_lines():
        line = raw.rstrip("\r")
        if line == "":
            if event_name == "home_event" and data_lines:
                payload = "\n".join(data_lines)
                handle_event(json.loads(payload))
            event_name = ""
            data_lines = []
        elif line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
        elif line.startswith(":"):
            pass  # keepalive comment
```

Event payload shape (mirrors `list_recent_events`):

```json
{
  "kind": "leak" | "smoke" | "contact_open" | "contact_close" | "lock_changed" | "vacuum_state",
  "entity_id": "binary_sensor.kitchen_leak",
  "friendly_name": "Kitchen Leak",
  "area": "kitchen",
  "state": "on",
  "previous_state": "off",
  "timestamp": "2026-05-02T10:07:01+00:00"
}
```

Motion sensors are intentionally excluded from this stream — they
fire too often to be useful as alerts. Use `list_devices` if you
need motion state.

The `/events` stream sends an initial `event: hello` so consumers
know it's alive. It also emits `: keepalive` comments every ~20 s
to defeat proxy idle timeouts.

---

## Patterns that work well

These come from running the kit against Gemma 4B, Claude Sonnet 4.6,
and GPT-4o-mini. They're not requirements — your agent will work
without them — but they make turns faster, more reliable, and feel
better.

### Inject the device list into your system prompt

Every smart-home turn starts with the agent needing to know what
exists. Calling `list_devices` adds an iteration and ~3 s of
prompt-processing time. Pre-fetch once at agent startup, slim the
result, and inject the entity-ID list into your system prompt:

```
Smart-home entity IDs you can use directly (no list_devices needed):
  living_room: light.living_room_main, light.living_room_accent (rgb), cover.living_room_blind
  bedroom: light.bedroom_main, light.bedroom_mood (rgb), sensor.bedroom_temperature, cover.bedroom_blind
  kitchen: light.kitchen_counter, switch.coffee_machine, ...
  ...
Saved scenes: scene.movie_night (Movie Night), scene.cozy_bedroom (Cozy Bedroom), ...
```

The reference integration (`home_agent_perf/app/tools/smart_home.py:_refresh_device_summary`) does this and saves ~3–5 s per typical turn.

### Prefer `apply_states` over multiple `turn_on` calls

For "set the kitchen for cooking" / "make it cozy in here" the
agent often picks several `turn_on` / `set_light` / `set_cover_position`
tools and dispatches them in parallel. That's two model iterations
(emit tools → wait for results → respond). `apply_states` collapses
to one tool call atomically, halving the wall time.

In your tool descriptions, lean into the `apply_states` framing for
multi-device requests.

### Use `apply_scene` for named atmospheres

If the user says "movie night" or "goodnight" or any scene name,
straight to `apply_scene` — don't list_devices first. The scene's
states apply atomically.

### Subscribe to `/events` for unprompted alerts

Don't poll `list_recent_events` on a timer. Open the SSE stream,
buffer events into your agent's context, surface them on the next
user interaction or via your own push channel.

### Cap the chat history

Long sessions otherwise inflate Gemma's prompt linearly per turn.
A 12–24 message cap (drop oldest non-system) is plenty for context
coherence and bounds the cost.

---

## Troubleshooting

| Symptom | Try |
| --- | --- |
| `streamablehttp_client` returns 307 | The MCP SDK handles redirects automatically — ignore the log lines |
| Tools list is empty | `make run-mcp` is running and `make bootstrap` ran successfully (HA token in `.env`) |
| `list_devices` is slow on first call | The MCP server is opening the HA WebSocket on first session — ~200 ms one-time |
| Tool call returns `{"error": "Unknown entity_id: ..."}` | Use the canonical entity_ids from `list_devices` (or the cheat sheet pattern above) |
| `/events` stream returns 404 | You're hitting `/mcp/events` — the events endpoint is at `/events`, not under `/mcp` |
| Connection drops after long idle | The `/events` stream sends keepalives but if your client buffers the response, increase its read timeout |

---

## Examples in this repo

- `scripts/smoketest_mcp.py` — minimal MCP discovery test
- `scripts/test_light_control.py` — end-to-end light control round-trip
- `scripts/test_milestone7.py` — control + behaviour observation
- `home_agent_perf/scripts/test_smart_home_chat.py` (sibling repo) —
  full LLM-driven turn through a streaming endpoint

These run against the live stack and double as integration examples.
