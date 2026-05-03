"""Topology workdir-override tests.

The simulator's `topology` module loads from the active topology.json
(workdir copy if present, package seed otherwise). These tests exercise
the path resolution at module-import time via subprocess + env var,
because the module's _DATA is computed once at import.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_with_workdir(workdir: Path, code: str) -> str:
    """Run a snippet of Python with SANDCASTLE_WORKDIR pointed at workdir."""
    env = {"SANDCASTLE_WORKDIR": str(workdir), "PATH": "/usr/bin:/bin"}
    # Forward the venv path so the package is importable.
    import os
    for k in ("VIRTUAL_ENV", "PYTHONPATH", "HOME"):
        if k in os.environ:
            env[k] = os.environ[k]
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_topology_uses_package_seed_when_workdir_empty(tmp_path):
    """No <workdir>/.sandcastle/topology.json → falls back to package seed."""
    out = _run_with_workdir(tmp_path, """
from sandcastle_sim.simulator import topology as t
print(t.total_devices())
""")
    # Package seed has 23 devices (22 user-visible + 1 vacuum aux sensor).
    assert int(out) == 23


def test_topology_prefers_workdir_when_seeded(tmp_path):
    """Custom workdir topology overrides the package seed."""
    state = tmp_path / ".sandcastle"
    state.mkdir()
    (state / "topology.json").write_text(json.dumps({
        "area_names": {"studio": "Studio"},
        "devices": {
            "light": [
                {"slug": "studio_main", "area": "studio",
                 "name": "Studio Main", "kind": "dimmable"}
            ]
        }
    }))
    out = _run_with_workdir(tmp_path, """
from sandcastle_sim.simulator import topology as t
print(t.total_devices(), list(t.AREA_NAMES.keys())[0])
""")
    n, area = out.split()
    assert int(n) == 1
    assert area == "studio"
