"""Packaging sanity checks the wheel-install CI step relies on.

These run against the *installed* package (so on CI we run them
inside the fresh venv that just `pip install`-ed the wheel). They
catch the kind of bugs we hit during the PyPI prep — broken
package-data globs, missing modules, wrong entry-point wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_version_exposed():
    import sandcastle_sim
    assert sandcastle_sim.__version__


def test_entry_modules_importable():
    """Every public sub-package must import cleanly. Lots of CI
    breakage starts as a quiet ImportError in one of these."""
    import sandcastle_sim.cli  # noqa: F401
    import sandcastle_sim.bootstrap  # noqa: F401
    import sandcastle_sim.runtime  # noqa: F401
    import sandcastle_sim.agent  # noqa: F401
    import sandcastle_sim.agent.chat  # noqa: F401
    import sandcastle_sim.agent.one_shot  # noqa: F401
    import sandcastle_sim.mcp_server.server  # noqa: F401
    import sandcastle_sim.simulator.main  # noqa: F401


def test_bundled_data_files_present():
    """The CLI's _data_path must resolve every seed the start
    command needs to copy out into a fresh workdir. A bad
    package-data glob in pyproject.toml (we hit this once already)
    surfaces here."""
    from sandcastle_sim.cli import _data_path
    expected = [
        ("gui", "index.html"),
        ("seeds", "docker-compose.yml"),
        ("seeds", "ha_config", "configuration.yaml"),
        ("seeds", "ha_config", "scenes.yaml"),
        ("seeds", "mosquitto", "config", "mosquitto.conf"),
    ]
    missing = [p for p in expected if not _data_path(*p).is_file()]
    assert not missing, f"missing bundled data files: {missing}"


def test_console_script_resolves():
    """`sandcastle-sim` should resolve via importlib.metadata —
    this catches a missing or misnamed [project.scripts] entry."""
    from importlib.metadata import entry_points
    eps = entry_points(group="console_scripts")
    names = {ep.name for ep in eps}
    assert "sandcastle-sim" in names
