"""Background-process orchestration for ``sandcastle-sim start/stop/logs``.

Two background components run alongside the Docker stack:

* ``sandcastle_sim.simulator`` — the device simulator + GUI host
* ``sandcastle_sim.mcp_server`` — the FastMCP server + SSE event push

We launch them with stdout/stderr redirected to log files, write PID
files alongside, and provide simple lifecycle helpers
(``start_component``, ``stop_component``, ``status_component``,
``tail_logs``). Cross-platform-ish: works on Linux, macOS, WSL2.

PID files and log files live under ``<workdir>/.sandcastle/``:

    .sandcastle/
      sim.pid    sim.log
      mcp.pid    mcp.log

The directory is created on first run. Stale PID files (where the
process is gone) are detected via ``os.kill(pid, 0)`` and treated
the same as "not running."

Why not systemd / launchd? For a dev kit, "tail of a log file"
beats "journalctl pipeline" for first-run debuggability.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Component:
    """One background process Sandcastle Sim manages."""

    name: str
    """Short id used for log/pid filenames (sim, mcp, ...)."""

    label: str
    """Human-readable label printed by the CLI."""

    module: str
    """Importable Python module to run with ``-m``."""

    port: int
    """Port the component listens on, for status checks."""


COMPONENTS: List[Component] = [
    Component("mcp", "MCP server",     "sandcastle_sim.mcp_server.server", 8765),
    Component("sim", "Simulator + GUI", "sandcastle_sim.simulator.main",   8766),
]


def _state_dir(workdir: Path) -> Path:
    d = workdir / ".sandcastle"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_file(workdir: Path, name: str) -> Path:
    return _state_dir(workdir) / f"{name}.pid"


def _log_file(workdir: Path, name: str) -> Path:
    return _state_dir(workdir) / f"{name}.log"


def _alive(pid: int) -> bool:
    """Cheap check: does this PID exist and is it ours to signal?"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def status_component(workdir: Path, comp: Component) -> Optional[int]:
    """Return the running PID, or None if not running.

    Cleans up stale PID files (process is gone, file remains).
    """
    pid_path = _pid_file(workdir, comp.name)
    if not pid_path.is_file():
        return None
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        pid_path.unlink(missing_ok=True)
        return None
    if not _alive(pid):
        pid_path.unlink(missing_ok=True)
        return None
    return pid


def start_component(workdir: Path, comp: Component, env: dict) -> int:
    """Spawn the component as a detached background process.

    No-op (returns the existing PID) if already running.
    """
    existing = status_component(workdir, comp)
    if existing:
        return existing

    log_path = _log_file(workdir, comp.name)
    pid_path = _pid_file(workdir, comp.name)
    # Append to existing log so prior runs are preserved one
    # session back; rotated externally if it gets unwieldy.
    log_f = open(log_path, "ab", buffering=0)
    log_f.write(
        b"\n=== sandcastle-sim: starting "
        + comp.name.encode()
        + b" at "
        + time.strftime("%Y-%m-%d %H:%M:%S").encode()
        + b" ===\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", comp.module],
        cwd=str(workdir),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        # Detach into its own session so SIGINT to the CLI parent
        # doesn't propagate to the children we just launched.
        start_new_session=True,
        close_fds=True,
    )
    pid_path.write_text(str(proc.pid))
    return proc.pid


def stop_component(
    workdir: Path,
    comp: Component,
    timeout: float = 10.0,
    progress: bool = False,
) -> str:
    """Stop a component gracefully (SIGTERM), escalating only if needed.

    Returns one of: ``"not running"``, ``"stopped"``, ``"killed"``.

    The simulator and MCP server both have signal handlers that
    propagate SIGTERM through their async event loops so context
    managers (MQTT client, aiohttp app, FastMCP lifespan) get a
    chance to disconnect cleanly. If a process ignores SIGTERM for
    longer than ``timeout`` seconds we escalate to SIGKILL — that's
    the unhealthy case and we report it so the user knows the
    shutdown wasn't clean.

    ``progress=True`` writes "." to stdout each second while waiting
    so the CLI can show liveness on slow shutdowns.
    """
    pid = status_component(workdir, comp)
    if pid is None:
        return "not running"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        _pid_file(workdir, comp.name).unlink(missing_ok=True)
        return "stopped"

    deadline = time.monotonic() + timeout
    last_dot = 0.0
    while time.monotonic() < deadline:
        if not _alive(pid):
            _pid_file(workdir, comp.name).unlink(missing_ok=True)
            return "stopped"
        if progress and (time.monotonic() - last_dot) >= 1.0:
            print(".", end="", flush=True)
            last_dot = time.monotonic()
        time.sleep(0.1)

    # Graceful window expired — process ignored SIGTERM. Escalate.
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _pid_file(workdir, comp.name).unlink(missing_ok=True)
    return "killed"


def tail_logs(workdir: Path, names: List[str], follow: bool = True, lines: int = 50) -> int:
    """Tail the named components' log files.

    Uses the system ``tail`` so users get the familiar -f behaviour
    with Ctrl-C terminating cleanly. If multiple components are
    requested, ``tail -f`` interleaves them.
    """
    paths = [_log_file(workdir, n) for n in names]
    paths = [p for p in paths if p.is_file()]
    if not paths:
        print("(no logs yet — nothing has been started)", file=sys.stderr)
        return 0
    cmd = ["tail", f"-n{lines}"]
    if follow:
        cmd.append("-f")
    cmd.extend(str(p) for p in paths)
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


def wait_for_port(host: str, port: int, timeout: float = 30.0, name: str = "") -> bool:
    """Block until a TCP listener answers on (host, port), or timeout."""
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            s.close()
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            pass
        finally:
            s.close()
        time.sleep(0.4)
    return False


def wait_for_http(url: str, timeout: float = 60.0) -> bool:
    """Block until ``GET <url>`` returns any 2xx/3xx, or timeout.

    Used for HA's slow first boot — the manifest endpoint is
    available before MQTT integration finishes loading, which is
    the right "is it talking to us yet" signal.
    """
    import httpx
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code < 400:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False
