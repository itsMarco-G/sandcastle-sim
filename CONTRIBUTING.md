# Contributing

Sandcastle Sim is an open-source initiative. The architecture is intentionally protocol-agnostic above Home Assistant so people with deep hardware expertise can land contributions cleanly. The maintainer doesn't claim Matter, Zigbee, Z-Wave, Thread, or BLE expertise; the kit's job is to be the common rendezvous point for those integrations.

## Especially welcome

- Protocol recipes: "what works with this hub", "tested devices", config patterns
- Hardware adapter shims for non-HA-native devices (custom serial, USB, proprietary cloud APIs)
- Floor-plan visual contributions (badges, drag-positioning, themes)
- Eval cases: golden prompts that broaden the regression net
- Documentation improvements, bug reports, reproductions

## Getting started

```sh
git clone git@github.com:itsMarco-G/sandcastle-sim.git
```

```sh
cd sandcastle-sim
pip install -e ".[dev]"
pytest tests/
```

For coding-agent contributors (Claude Code, Codex, Copilot, ...), [AGENTS.md](AGENTS.md) explains the repo layout, conventions, and the eval-suite workflow that's expected before reporting work as done.

## Running the integration suite

The unit tests above run without infrastructure. The full integration suite needs a live stack:

```sh
sandcastle-sim start
```

```sh
pytest tests/test_integration_smoke.py -m integration
```

## Pull request guidelines

- Run `pytest tests/` before submitting; integration tests run automatically in the release-check workflow
- Run `sandcastle-sim eval --save-baseline` before changes, `sandcastle-sim eval --diff` after, to catch agent-quality regressions
- Match the existing code style; no formatter is enforced but consistency matters
- Add tests for behavioural changes
- Keep PRs focused; one change per PR

## Where to ask

Open an issue or PR at https://github.com/itsMarco-G/sandcastle-sim. If you're not sure where something fits, open an issue first and we can figure it out together.
