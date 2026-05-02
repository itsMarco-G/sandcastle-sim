# Sandcastle Sim — orchestration shortcuts.
#
# Most workflows are done via the `sandcastle-sim` CLI (after
# `pip install -e .` or `pip install sandcastle-sim`):
#
#   sandcastle-sim up
#   sandcastle-sim bootstrap
#   sandcastle-sim sim     # foreground
#   sandcastle-sim mcp     # foreground (different terminal)
#   sandcastle-sim "turn off the kitchen light"
#
# This Makefile keeps the older `make` shortcuts working for muscle
# memory, plus adds a few that don't fit cleanly into the CLI
# (test, wipe-sim, normalize-entities, clean-ha).

VENV := .venv
PY := $(VENV)/bin/python
SANDCASTLE := $(VENV)/bin/sandcastle-sim

.PHONY: help up down start stop restart logs ps wait-ha bootstrap seed-light \
        venv install run-mcp run-sim wipe-sim normalize-entities \
        test-light test-mcp clean-ha status agent

help:
	@echo "Sandcastle Sim — make targets:"
	@echo ""
	@echo "  Headline:"
	@echo "    make venv               Create .venv and install sandcastle-sim editable"
	@echo "    make start              Bring everything up (Mosquitto + HA + sim + MCP)"
	@echo "    make stop               Gracefully tear everything down"
	@echo "    make status             Show what's running"
	@echo ""
	@echo "  Drive it with the agent:"
	@echo "    make agent CMD='turn off the kitchen light'"
	@echo ""
	@echo "  Component-level (manual control):"
	@echo "    make up                 Start Mosquitto + HA only"
	@echo "    make wait-ha            Block until HA's HTTP API is responsive"
	@echo "    make bootstrap          Onboard HA (writes .env)"
	@echo "    make run-mcp            MCP server (foreground)"
	@echo "    make run-sim            Simulator + GUI (foreground)"
	@echo "    make down               Stop the Docker stack only"
	@echo "    make logs               Tail container logs"
	@echo ""
	@echo "  Maintenance:"
	@echo "    make wipe-sim           Clear retained sim_* MQTT discovery messages"
	@echo "    make normalize-entities Force HA entity_ids to contract slugs (defensive)"
	@echo "    make test-mcp           Smoke-test discovery tools end-to-end"
	@echo "    make test-light         Acceptance test: control round-trip"
	@echo "    make clean-ha           WIPE HA config (use when onboarding state is bad)"
	@echo ""
	@echo "  Or use the CLI directly:  sandcastle-sim --help"

start:
	$(SANDCASTLE) start

stop:
	$(SANDCASTLE) stop

# --- Quickstart targets (delegate to the CLI where possible) ---

venv:
	test -d $(VENV) || python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e .

install: venv

up:
	$(SANDCASTLE) up

down:
	$(SANDCASTLE) down

status:
	$(SANDCASTLE) status

restart:
	docker compose restart

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps

wait-ha:
	@echo "Waiting for HA on http://localhost:8123 ..."
	@for i in $$(seq 1 60); do \
	  code=$$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8123/manifest.json); \
	  if [ "$$code" = "200" ]; then \
	    echo "HA up after $${i}*3s (HTTP $$code)"; exit 0; \
	  fi; \
	  sleep 3; \
	done; \
	echo "HA didn't come up in 180s — check 'make logs'"; exit 1

bootstrap: wait-ha
	$(SANDCASTLE) bootstrap

run-mcp:
	$(SANDCASTLE) mcp

run-sim:
	$(SANDCASTLE) sim

# Run a prompt through the built-in agent. Usage:
#   make agent CMD='turn off the kitchen light'
agent:
	@if [ -z "$(CMD)" ]; then \
	  echo 'usage: make agent CMD="your prompt here"' >&2; exit 2; \
	fi
	$(SANDCASTLE) "$(CMD)"

# --- Maintenance / debug ---

# Publish one HA-MQTT-discovery message to validate the broker chain.
seed-light:
	docker compose exec -T mosquitto mosquitto_pub \
	  -h localhost -p 1883 -r \
	  -t 'homeassistant/light/test_kitchen/config' \
	  -m '{"name":"Test Kitchen Light","unique_id":"test_kitchen_main","schema":"json","command_topic":"homeassistant/light/test_kitchen/set","state_topic":"homeassistant/light/test_kitchen/state","brightness":true,"supported_color_modes":["brightness"]}'
	docker compose exec -T mosquitto mosquitto_pub \
	  -h localhost -p 1883 -r \
	  -t 'homeassistant/light/test_kitchen/state' \
	  -m '{"state":"OFF"}'
	@echo "Seeded light.test_kitchen — give HA ~2s to register."

wipe-sim:
	@docker compose exec -T mosquitto mosquitto_sub -h localhost -p 1883 -t '#' -W 3 -F "%t" 2>/dev/null \
	  | grep -E '/sim_' | sort -u > /tmp/sim_topics.txt || true
	@COUNT=$$(wc -l < /tmp/sim_topics.txt); echo "wiping $$COUNT retained sim_ topics..."; \
	while read -r t; do [ -n "$$t" ] && \
	  docker compose exec -T mosquitto mosquitto_pub -h localhost -p 1883 -r -t "$$t" -m '' 2>/dev/null; \
	done < /tmp/sim_topics.txt
	@echo "done"

normalize-entities:
	set -a && . ./.env && set +a && $(PY) scripts/normalize_entity_ids.py

test-mcp:
	$(PY) scripts/smoketest_mcp.py

test-light:
	$(PY) scripts/test_light_control.py

clean-ha:
	docker compose down
	rm -rf ha-config/.storage ha-config/.cloud ha-config/home-assistant.log* \
	       ha-config/home-assistant_v2.db* ha-config/secrets.yaml \
	       ha-config/automations.yaml ha-config/scripts.yaml \
	       ha-config/.HA_VERSION ha-config/blueprints \
	       ha-config/tts ha-config/deps ha-config/custom_components 2>/dev/null || true
	@echo "HA config wiped (kept configuration.yaml + scenes.yaml). Run 'make up && make bootstrap' to redo."
