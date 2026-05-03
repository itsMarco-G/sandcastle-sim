# Adding a Matter device

The smart-home demo is built so adding a real Matter device is a
config change, not a code change. Nothing above Home Assistant
(MCP server, simulator, GUI, agent) speaks any device protocol
directly — they all talk to HA, and HA abstracts MQTT, Matter,
Zigbee, and the rest behind a common entity model.

This document is the runbook for actually enabling Matter and
commissioning a device. **No code in this repo changes.** The new
device shows up in `list_devices` with `protocol: "matter"`, the
GUI renders it with a `MATTER` badge instead of `MQTT`, and the
agent controls it with the same tools (`turn_on`, `set_light`,
`lock`, ...).

## Prerequisites

- A Matter-capable device. Most 2023+ smart bulbs, plugs, and locks
  ship Matter-over-Wi-Fi or Thread-over-Matter. The QR / pairing
  code on the device is what HA needs.
- A Matter controller. The
  [`python-matter-server`](https://github.com/home-assistant-libs/python-matter-server)
  project provides one as a Docker image and is what HA's official
  Matter integration talks to.
- For Thread devices: a Thread border router (the Apple TV 4K, Nest
  Hub 2nd gen, eero 6+, etc. work). For Wi-Fi-only Matter devices,
  no extra hardware needed.

## 1. Enable the matter-server container

Add the matter-server service to `docker-compose.yml`:

```yaml
services:
  # ... mosquitto, homeassistant ...

  matter-server:
    image: ghcr.io/home-assistant-libs/python-matter-server:stable
    container_name: smart_home_matter_server
    restart: unless-stopped
    network_mode: host        # required: Matter uses mDNS, multicast, IPv6
    volumes:
      - ./matter-data:/data
    environment:
      - LOG_LEVEL=info
```

Note `network_mode: host` — Matter commissioning needs raw access
to mDNS / IPv6, which Docker bridge networking blocks. This is the
same constraint HassOS uses; it's not specific to this setup.

```bash
make up
```

The matter-server now listens on `localhost:5580` (its WebSocket
API).

## 2. Add the Matter integration to Home Assistant

Open the HA web UI at `http://localhost:8123` and go to
**Settings → Devices & services → Add integration → Matter**.

When prompted for the matter-server URL, enter:

```
ws://localhost:5580/ws
```

HA accepts this and the integration loads. No restart needed.

Equivalent API call (if you'd rather automate, mirroring how the
bootstrap script sets up MQTT):

```bash
. .env
curl -s -X POST -H "Authorization: Bearer $HA_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"handler":"matter"}' \
  http://localhost:8123/api/config/config_entries/flow

# Then submit the flow's broker step with matter-server URL.
# See scripts/bootstrap_ha.py for the pattern.
```

## 3. Commission a device

In the HA UI: **Settings → Devices & services → Matter → Add device**.

Either:

- Scan the device's QR code (HA opens the camera if you're on a
  phone, or you can paste the QR's text payload), or
- Type the manual setup code printed on the device.

HA performs the Matter commissioning handshake (key exchange,
fabric join, attestation). This takes 30–90 seconds for most
devices. When it succeeds, HA creates entities for every cluster
the device exposes — typically `light.<vendor_model>` for a bulb,
`switch.<vendor_model>` for a plug, `lock.<vendor_model>` for a
lock, etc.

## 4. Assign an area (optional but worth it)

Pick the new device in the HA UI and assign it to one of the demo's
areas: `living_room`, `kitchen`, `hallway`, `bedroom`, `bedroom_2`,
or `bathroom`. The MCP server's `list_devices` will then return the
device with `area: "<key>"` and the floor plan will be able to
render it in the right room.

If the device's friendly name isn't what you want in the GUI, rename
it in HA — the MCP server reads `friendly_name` from the entity
attributes.

## 5. Verify the round-trip

```bash
# List MCP-visible devices and confirm the new one carries protocol: matter
.venv/bin/python -c "
import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main():
    async with streamablehttp_client('http://localhost:8765/mcp/') as (r,w,_):
        async with ClientSession(r,w) as s:
            await s.initialize()
            res = await s.call_tool('list_devices', {})
            for d in json.loads(res.content[0].text)['devices']:
                if d['protocol'] == 'matter':
                    print(json.dumps(d, indent=2))
asyncio.run(main())
"
```

The agent can now drive the device exactly like any simulated one:

> *"Turn on the bedroom matter bulb."*

Click the device on the floor plan to toggle it interactively. The
event panel and any pushed `home_event` notifications work
identically. **No code in `tools/smart_home_mcp/`,
`simulator/`, or `gui/` changes** — that's the whole point.

## 6. Adding the GUI position (one-time)

Two ways:

**Easiest — run the auto-layout.** With the device live in HA:

```bash
sandcastle-sim floorplan auto
```

The deterministic layout places the new device based on its type
(light → spread along the upper third; sensor → near a wall;
contact → bottom edge; etc.) without touching anything already in the
file. See [`docs/floorplan.md`](floorplan.md) for the full vocabulary
and how to nudge a placement afterwards.

**Manual.** Edit `<workdir>/.sandcastle/floorplan.json` (your home;
created on first `sandcastle-sim start` from the bundled default)
and add one entry under `devices`:

```json
"light.eve_energy_strip_42": {
  "area": "living_room", "type": "light", "x": 100, "y": 240
}
```

(`x` / `y` are room-local pixels — see existing entries for ranges.
The light renderer handles RGB / dimmable / on-off automatically based
on `attributes.supported_color_modes`.)

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| HA says "Cannot connect to Matter server" | Container not running, or you used `localhost:5580` from inside another container — `host` networking should fix this |
| Commissioning times out at "Discovering" | Device not in pairing mode, or your machine isn't on the same network segment. Reset the device and retry. |
| Device appears but state is "unavailable" | Wi-Fi credentials weren't shared during commissioning, or the Thread border router isn't reachable. Check the matter-server logs |
| MCP server returns the device but `protocol` says something other than `"matter"` | The integration created the entity but `entity_entry.platform` reports the underlying transport (e.g., `mqtt` for a Matter-bridged Thread device routed through Z2M). Look at the entity's device-info in HA to see what's underneath |

## What this proves

The agent and GUI control a Matter device without knowing it's
Matter. The MCP server's `list_devices` reports `protocol: "matter"`
because that's what HA's entity registry says — the GUI's
`MATTER` badge swap is purely cosmetic. Replacing the simulator
entirely with real Matter / Zigbee / Z-Wave devices follows the
same pattern: enable the integration in HA, commission, optionally
assign an area + GUI position, done.

The architecture's Matter-readiness was a deliberate design choice
in milestone 2 (see `architecture.md`); this runbook is the
demonstration.
