"""sandcastle-sim — command-line entry point.

Two layers:

* **Subcommands** wrap the workflow:
  * ``up`` — start Mosquitto + Home Assistant containers
  * ``bootstrap`` — onboard HA + create areas + mint long-lived token
  * ``sim`` — run the device simulator + floor-plan GUI (foreground)
  * ``mcp`` — run the MCP server (foreground)
  * ``status`` — show what's running on the demo's ports
  * ``down`` — stop the containers
  * ``agent`` — run a single prompt through the built-in Ollama+MCP agent

* **Bare invocation** ``sandcastle-sim "turn off the kitchen light"``
  is shorthand for ``sandcastle-sim agent "turn off the kitchen light"``.
  It's the dev-friendly path: pip-install the kit, run one prompt,
  see it work.

The CLI is deliberately small — most subcommands shell out to
docker-compose or to module entry points. It's a thin convenience
layer, not a daemon.
"""

from __future__ import annotations

import argparse
import itertools
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def _data_path(*parts: str) -> Path:
    """Return the absolute path to a file shipped inside the package."""
    base = resources.files("sandcastle_sim").joinpath("data")
    p = base
    for part in parts:
        p = p.joinpath(part)
    return Path(str(p))


def _running_in_repo_checkout() -> bool:
    """Heuristic: are we in a git checkout of this repo?

    True when ``cwd/docker-compose.yml`` exists alongside an
    ``ha-config/`` directory. False for a pip-installed user.
    """
    cwd = Path.cwd()
    return (cwd / "docker-compose.yml").is_file() and (cwd / "ha-config").is_dir()


def _ensure_workdir(workdir: Path) -> Path:
    """Bootstrap a writable workdir for compose + HA config + Mosquitto.

    Pip-installed users don't have the repo's top-level files. We
    materialise a minimal layout in either the user's cwd or
    ``~/.local/share/sandcastle-sim``: copy the seed
    docker-compose.yml, ha_config/, mosquitto/ from package data.

    Initial setup is idempotent — only copies missing files, never
    overwrites user-edited configs. After that, ``_merge_new_scenes``
    additively pulls in any scenes the bundle has gained since the
    workdir was created (so an upgrade from 0.1.0 picks up new
    bundled scenes like ``welcome_guest`` without touching anything
    the user edited locally).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    seeds = _data_path("seeds")

    targets = {
        seeds / "docker-compose.yml": workdir / "docker-compose.yml",
        seeds / "ha_config" / "configuration.yaml": workdir / "ha-config" / "configuration.yaml",
        seeds / "ha_config" / "scenes.yaml": workdir / "ha-config" / "scenes.yaml",
        seeds / "mosquitto" / "config" / "mosquitto.conf": workdir / "mosquitto" / "config" / "mosquitto.conf",
    }
    for src, dst in targets.items():
        if dst.exists():
            continue
        if not src.exists():
            print(f"warning: package seed missing at {src}", file=sys.stderr)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  + {dst.relative_to(workdir)}")

    added = _merge_new_scenes(
        seeds / "ha_config" / "scenes.yaml",
        workdir / "ha-config" / "scenes.yaml",
    )
    if added:
        _try_reload_ha_scenes(workdir)

    return workdir


def _merge_new_scenes(seed: Path, dst: Path) -> List[str]:
    """Add any scenes the bundle has that the workdir doesn't.

    Compares by ``id``. Existing scenes are never modified — if the
    user edited ``movie_night``'s entities locally we leave that
    alone — but new ids (``welcome_guest`` shipped in 0.1.1, plus
    anything later) are appended verbatim. Returns the list of ids
    that were added.
    """
    if not seed.is_file() or not dst.is_file():
        return []
    try:
        import yaml  # local import — yaml is already a runtime dep
    except ImportError:  # pragma: no cover
        return []

    try:
        seed_scenes = yaml.safe_load(seed.read_text()) or []
        dst_scenes = yaml.safe_load(dst.read_text()) or []
    except yaml.YAMLError:
        return []
    if not isinstance(seed_scenes, list) or not isinstance(dst_scenes, list):
        return []

    have = {s.get("id") for s in dst_scenes if isinstance(s, dict)}
    new = [
        s for s in seed_scenes
        if isinstance(s, dict) and s.get("id") and s["id"] not in have
    ]
    if not new:
        return []

    added_ids = [s["id"] for s in new]
    # Append by re-dumping the full list (preserves YAML quoting better
    # than concatenating raw text, and survives a one-off blank line).
    merged = dst_scenes + new
    dst.write_text(yaml.safe_dump(merged, sort_keys=False))
    print(f"  + ha-config/scenes.yaml: added {', '.join(added_ids)}")
    return added_ids


def _try_reload_ha_scenes(workdir: Path) -> None:
    """If HA is already running, tell it to reload scenes.yaml so
    newly-merged scenes show up without a full restart. Silent
    no-op on cold start (HA will load the file naturally on boot)
    or any networking / auth hiccup — never blocks bootstrap."""
    env_path = workdir / ".env"
    if not env_path.is_file():
        return
    token = ""
    url = "http://localhost:8123"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if k.strip() == "HA_TOKEN":
            token = v
        elif k.strip() == "HA_URL" and v:
            url = v
    if not token:
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{url.rstrip('/')}/api/services/scene/reload",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
            data=b"{}",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            if 200 <= resp.status < 300:
                print("  · HA scenes reloaded")
    except Exception:
        # HA isn't up yet, or token is stale, or network blip — fine.
        pass


def _resolve_workdir(workdir_arg: Optional[str]) -> Path:
    """Pick the right working directory based on context.

    Order:
      1. Explicit --workdir arg wins.
      2. If we're in a repo checkout (top-level docker-compose.yml
         + ha-config/), use cwd.
      3. Else use ~/.local/share/sandcastle-sim and seed it.
    """
    if workdir_arg:
        return Path(workdir_arg).resolve()
    if _running_in_repo_checkout():
        return Path.cwd()
    return Path.home() / ".local" / "share" / "sandcastle-sim"


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if a TCP listener is bound to (host, port)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((host, port))
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False
    finally:
        s.close()


def _find_port_pid(port: int) -> Optional[int]:
    """Return the PID listening on port, or None if not found / lsof unavailable."""
    try:
        r = subprocess.run(
            ["lsof", "-t", f"-i:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return int(r.stdout.strip().split()[0])
    except Exception:
        pass
    return None


def _evict_port(port: int) -> Optional[int]:
    """SIGTERM (then SIGKILL) whatever is listening on port. Returns evicted PID."""
    pid = _find_port_pid(port)
    if pid is None:
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return pid
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _port_open(port):
        time.sleep(0.2)
    if _port_open(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.3)
    return pid


# --------------------------------------------------------------------------- #
# Progress UI                                                                 #
# --------------------------------------------------------------------------- #


class _Spinner:
    """Tiny inline spinner for long-running steps.

    Thread-driven (the work runs on the main thread, the spinner ticks
    on a daemon). Falls back to plain "..." if stderr isn't a TTY so
    log files don't fill with control characters.
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str, stream=sys.stderr):
        self.label = label
        self.stream = stream
        self.tty = stream.isatty()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_time: float = 0.0

    def __enter__(self) -> "_Spinner":
        self._start_time = time.monotonic()
        if self.tty:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            self.stream.write(f"  {self.label} ...")
            self.stream.flush()
        return self

    def _spin(self) -> None:
        # In-progress tick: cyan braille frame, plain label, dim elapsed.
        # Raw ANSI keeps the hot loop allocation-free vs. Rich Console.
        for frame in itertools.cycle(self._FRAMES):
            if self._stop.is_set():
                return
            elapsed = time.monotonic() - self._start_time
            self.stream.write(
                f"\r  \033[36m{frame}\033[0m {self.label} "
                f"\033[2m({elapsed:4.1f}s)\033[0m"
            )
            self.stream.flush()
            time.sleep(0.08)

    def finish(self, status: str = "done", ok: bool = True) -> None:
        from rich.console import Console

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        elapsed = time.monotonic() - self._start_time

        if self.tty:
            # Clear the spinner line, then let Rich print the final
            # state on the now-empty row.
            self.stream.write("\r\033[K")
            self.stream.flush()
        else:
            self.stream.write(" ")

        mark = "[bold green]✓[/]" if ok else "[bold red]✗[/]"
        # Status text turns red on failure so the eye lands on it
        # instead of having to parse the marker first.
        status_style = "" if ok else "[red]"
        status_close = "" if ok else "[/]"
        Console(file=self.stream, highlight=False).print(
            f"  {mark} [bold]{self.label}[/] [dim]—[/] "
            f"{status_style}{status}{status_close} "
            f"[dim]({elapsed:.1f}s)[/]"
        )

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._stop.is_set():
            self.finish("error" if exc else "done", ok=exc is None)


# --------------------------------------------------------------------------- #
# Castle banner for start / stop                                              #
# --------------------------------------------------------------------------- #
#
# A single big sandcastle with a face, rendered as a Rich Panel
# (cyan border, "Sandcastle Sim" title) with sand-yellow castle
# lines and cyan waves. Shown as the success confirmation —
# `start` prints it after every component is up, `stop` after the
# Docker stack is gone — so seeing it == green light.

_CASTLE_LINES = [
    "         |>>>                        |>>>",
    "         |                           |",
    "         |     _________________     |",
    "         |    |  _ _ _ _ _ _ _  |    |",
    "         |    | |             | |    |",
    "         |    | |   ◕  ‿  ◕   | |    |",
    "       _|_|_  | |_____________| |  _|_|_",
    "      |     |_|_________________|_|     |",
    "      | [ ]  _                   _  [ ] |",
    "      |     | |      _____      | |     |",
    "      |     |_|     /     \\     |_|     |",
    "      |  _  |      |       |      |  _  |",
    "      |_|_|_|______|_______|______|_|_|_|",
    "     /                                   \\",
]

_WAVE_LINES = [
    "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
    "  ~     ~     ~     ~     ~     ~     ~     ~",
]


def _print_castle(tagline: str, *, next_url: Optional[str] = None) -> None:
    """Render the castle inside a Rich Panel with sand/sea colors.

    Castle body is gold1 (sand), waves are cyan (sea). The tagline
    appears below the art in bold inside the same panel.

    If ``next_url`` is provided, the URL is appended inside the
    same panel as the prominent next action (start uses this; stop
    does not). Keeps the success confirmation and the call to
    action together in one bordered block instead of two stacked
    panels that push the castle off-screen.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    body = Text()
    for line in _CASTLE_LINES:
        body.append(line + "\n", style="gold1")
    for line in _WAVE_LINES:
        body.append(line + "\n", style="cyan")
    body.append("\n  ")
    body.append(tagline, style="bold")

    if next_url:
        body.append("\n\n  ")
        body.append("▶ Open ", style="bold green")
        body.append(next_url, style="bold bright_yellow underline")
        body.append("\n     the floor plan — click any device to control it",
                    style="dim")

    Console().print(
        Panel(
            body,
            title="[bold cyan]Sandcastle Sim[/]",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
            expand=False,
        )
    )


def _diagnose_port_conflict(workdir: Path) -> Optional[str]:
    """If 1883 or 8123 is busy and not held by our compose stack, explain.

    Returns a multi-line user-facing message, or None if no conflict.
    """
    busy: List[tuple[int, str]] = []
    for port, label in [(1883, "Mosquitto"), (8123, "Home Assistant")]:
        if _port_open(port):
            busy.append((port, label))
    if not busy:
        return None

    # Check whether docker compose already owns the ports — that's
    # not a conflict, that's just "already running".
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=str(workdir), capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and "homeassistant" in result.stdout:
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    is_macos = sys.platform == "darwin"
    lines = [
        "Port conflict — something else is already listening:",
    ]
    for port, label in busy:
        lines.append(f"  • port {port} ({label}) is in use")
    lines.append("")
    lines.append("Find what's holding the port:")
    if is_macos:
        lines.append(f"  lsof -nP -iTCP:{busy[0][0]} -sTCP:LISTEN")
    else:
        lines.append(
            f"  lsof -i:{busy[0][0]}    # or: ss -ltnp 'sport = :{busy[0][0]}'"
        )
    lines.append("")
    lines.append("Common causes:")
    if is_macos:
        lines.append("  • Homebrew Mosquitto running on 1883")
        lines.append("    (stop it: brew services stop mosquitto)")
    else:
        lines.append("  • A system-installed Mosquitto running on 1883")
        lines.append("    (stop it: sudo systemctl stop mosquitto)")
    lines.append("  • A previous Sandcastle stack from another workdir")
    lines.append("    (stop it: sandcastle-sim stop --workdir <that-dir>)")
    lines.append("  • A leftover Docker container from a prior run")
    lines.append("    (find it: docker ps | grep -E '1883|8123')")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Subcommand handlers                                                         #
# --------------------------------------------------------------------------- #


def cmd_up(args: argparse.Namespace) -> int:
    """Start Mosquitto + HA containers via docker compose."""
    workdir = _resolve_workdir(args.workdir)
    if not _running_in_repo_checkout():
        print(f"sandcastle-sim: using workdir {workdir}")
        _ensure_workdir(workdir)

    cmd = ["docker", "compose", "up", "-d"]
    print(f"$ {' '.join(cmd)}  (in {workdir})")
    return subprocess.call(cmd, cwd=str(workdir))


def cmd_down(args: argparse.Namespace) -> int:
    """Stop Mosquitto + HA containers (volumes preserved)."""
    workdir = _resolve_workdir(args.workdir)
    cmd = ["docker", "compose", "down"]
    print(f"$ {' '.join(cmd)}  (in {workdir})")
    return subprocess.call(cmd, cwd=str(workdir))


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Onboard HA, create areas, mint a long-lived token, set up MQTT.

    Idempotent — re-runs are safe and skip already-done steps.
    """
    workdir = _resolve_workdir(args.workdir)
    cmd = [sys.executable, "-m", "sandcastle_sim.bootstrap"]
    print(f"$ {' '.join(cmd)}  (in {workdir})")
    return subprocess.call(cmd, cwd=str(workdir))


# Files inside ha-config/ that hold HA's persistent runtime state. These
# are what survives `docker compose down -v` (because ha-config is a
# bind-mount, not a named volume) and what causes the "user already
# onboarded, no token in .env" wedge that bootstrap can't recover from.
# Tracked configs (configuration.yaml, scenes.yaml) are preserved.
_HA_STATE_PATHS = (
    ".storage", ".cloud", "blueprints", "deps", "tts", "custom_components",
    ".HA_VERSION", ".ha_run.lock",
    "home-assistant_v2.db", "home-assistant_v2.db-shm", "home-assistant_v2.db-wal",
    "home-assistant.log", "home-assistant.log.1", "home-assistant.log.fault",
    "secrets.yaml", "automations.yaml", "scripts.yaml",
)


def cmd_reset(args: argparse.Namespace) -> int:
    """Wipe HA's persisted state so the next start is a clean bootstrap.

    The ha-config/ directory is bind-mounted into HA's ``/config`` so
    users can edit ``scenes.yaml`` directly. Side effect: HA writes its
    DB, ``.storage``, onboarding state, etc. into the same folder, owned
    by root (the in-container user). ``docker compose down -v`` only
    wipes named volumes, so those files survive — and a stale
    ``.storage`` against a missing ``.env`` token leaves bootstrap
    permanently wedged.

    This command stops the stack, runs an Alpine one-shot in Docker to
    delete the root-owned state files (no host sudo required), removes
    the Mosquitto named volume, and drops ``.env``. Tracked configs
    (``configuration.yaml``, ``scenes.yaml``) are preserved.
    """
    workdir = _resolve_workdir(args.workdir)
    ha_config = workdir / "ha-config"

    if not ha_config.is_dir():
        print(f"No ha-config/ at {ha_config} — nothing to reset.")
        return 0

    if not args.yes:
        from rich.console import Console
        Console().print(
            f"\n[bold yellow]This will wipe HA's persisted state at "
            f"{ha_config}[/]:\n"
            "  - admin user, onboarding, auth tokens (.storage)\n"
            "  - HA database (home-assistant_v2.db)\n"
            "  - blueprints, deps, tts, custom_components\n"
            "  - logs and version markers\n"
            "  - .env in the workdir (so bootstrap mints a fresh token)\n"
            "  - mosquitto data volume\n\n"
            "[bold]Tracked configs (configuration.yaml, scenes.yaml) "
            "are preserved.[/]\n"
        )
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    # 1. Bring everything down first so HA isn't writing while we wipe.
    print("\nStopping stack ...")
    subprocess.call(
        ["docker", "compose", "down", "-v"],
        cwd=str(workdir),
    )

    # 2. Wipe HA state via an Alpine one-shot — root inside the container
    #    can rm root-owned files on the bind mount without host sudo.
    paths_to_wipe = " ".join(f"/wipe/{p}" for p in _HA_STATE_PATHS)
    print("Wiping HA persisted state ...")
    rc = subprocess.call(
        [
            "docker", "run", "--rm",
            "-v", f"{ha_config.resolve()}:/wipe",
            "alpine:3", "sh", "-c", f"rm -rf {paths_to_wipe}",
        ],
        cwd=str(workdir),
    )
    if rc != 0:
        print(
            f"docker run exited {rc}. ha-config/ state may be partially "
            "wiped. Re-run `sandcastle-sim reset --yes` once Docker is "
            "healthy.",
            file=sys.stderr,
        )
        return rc

    # 3. Drop .env so bootstrap regenerates a fresh HA_TOKEN.
    env_path = workdir / ".env"
    if env_path.is_file():
        env_path.unlink()
        print(f"Removed {env_path}")

    print("\n[ok] HA state wiped. Next: sandcastle-sim start")
    return 0


def cmd_sim(args: argparse.Namespace) -> int:
    """Run the device simulator + floor-plan GUI server (foreground)."""
    workdir = _resolve_workdir(args.workdir)
    env = os.environ.copy()
    env["SANDCASTLE_WORKDIR"] = str(workdir)
    # Ensure .env loads from the workdir's dotfile if present.
    if (workdir / ".env").is_file():
        env.setdefault("DOTENV_PATH", str(workdir / ".env"))

    print(f"sandcastle-sim sim   (workdir {workdir})")
    print("  GUI:           http://localhost:8766")
    print("  demo trigger:  POST http://localhost:8766/api/demo/trigger")
    print("  Ctrl-C to stop.\n")
    cmd = [sys.executable, "-m", "sandcastle_sim.simulator.main"]
    return subprocess.call(cmd, cwd=str(workdir), env=env)


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run the smart-home MCP server (foreground)."""
    workdir = _resolve_workdir(args.workdir)
    env = os.environ.copy()
    env["SANDCASTLE_WORKDIR"] = str(workdir)
    if (workdir / ".env").is_file():
        # Source vars from .env so HA_URL / HA_TOKEN are available.
        for line in (workdir / ".env").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    print(f"sandcastle-sim mcp   (workdir {workdir})")
    print("  MCP:    http://localhost:8765/mcp/")
    print("  events: http://localhost:8765/events  (SSE)")
    print("  Ctrl-C to stop.\n")
    cmd = [sys.executable, "-m", "sandcastle_sim.mcp_server.server"]
    return subprocess.call(cmd, cwd=str(workdir), env=env)


def cmd_start(args: argparse.Namespace) -> int:
    """One-shot bring-up: docker stack + bootstrap + sim + MCP.

    The headline first-run flow. After this returns the user can:
      * open http://localhost:8766 to see the floor plan
      * click on devices to fiddle manually
      * run ``sandcastle-sim "your prompt"`` once they've also
        got Ollama serving a tool-use model

    Idempotent — already-running components are skipped, prior
    bootstrap is detected, sim and MCP that are already up stay up.
    """
    from . import runtime

    workdir = _resolve_workdir(args.workdir)
    if not _running_in_repo_checkout():
        print(f"Workdir: {workdir}")
        _ensure_workdir(workdir)

    # Seed the user's home (floorplan + topology) into <workdir>/.sandcastle/.
    # Idempotent: only copies missing files, never overwrites edits.
    # Runs in both repo-checkout and pip-install modes — the user's
    # customisations live here regardless of how the kit was installed.
    from .floorplan import seed_workdir
    seed_summary = seed_workdir(workdir)
    if seed_summary["created"]:
        print(f"  + .sandcastle/{', .sandcastle/'.join(seed_summary['created'])}")

    from rich.console import Console
    Console().print("[bold cyan]Starting Sandcastle Sim ...[/]")

    # Pre-flight: catch port conflicts before docker compose's
    # cryptic "address already in use" buries the cause.
    conflict = _diagnose_port_conflict(workdir)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1

    # 1. Docker stack (Mosquitto + HA).
    with _Spinner("Mosquitto + Home Assistant (Docker)") as sp:
        proc = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            sp.finish("failed", ok=False)
            stderr = (proc.stderr or "").strip()
            if "address already in use" in stderr or "port is already allocated" in stderr:
                # Re-run the diagnostic now that compose has surfaced it.
                msg = _diagnose_port_conflict(workdir) or stderr
                print(msg, file=sys.stderr)
            elif "Cannot connect to the Docker daemon" in stderr:
                hint = (
                    "Open Docker Desktop." if sys.platform == "darwin"
                    else "Start Docker Desktop or `sudo systemctl start docker`."
                )
                print(f"Docker daemon isn't running. {hint}", file=sys.stderr)
            else:
                print("docker compose up failed:", file=sys.stderr)
                if stderr:
                    print(stderr, file=sys.stderr)
            return proc.returncode

    # 2. Wait for HA to come online (slow first boot is normal).
    with _Spinner("Home Assistant ready") as sp:
        ok = runtime.wait_for_http(
            "http://localhost:8123/manifest.json", timeout=180,
        )
        if not ok:
            sp.finish("timeout after 180s", ok=False)
            print(
                "  Check `sandcastle-sim logs ha` or "
                "`docker compose logs homeassistant`.",
                file=sys.stderr,
            )
            return 1

    # 3. Bootstrap if needed (idempotent — script self-detects).
    with _Spinner("HA onboarding + MQTT integration") as sp:
        rc = _run_bootstrap(workdir, quiet=True)
        if rc != 0:
            sp.finish("failed", ok=False)
            print(
                "  Rerun with `sandcastle-sim bootstrap` to see what failed.",
                file=sys.stderr,
            )
            return rc

    # 4. Spawn simulator + MCP server in the background.
    env = _env_with_dotenv(workdir)
    for comp in runtime.COMPONENTS:
        existing = runtime.status_component(workdir, comp)
        if existing:
            Console().print(
                f"  [bold green]✓[/] [bold]{comp.label}[/] [dim]—[/] "
                f"already running [dim](PID {existing})[/]"
            )
            continue
        # Port occupied by a process we've lost track of (e.g. a stale server
        # that survived a previous reset). Evict it so the fresh component can
        # bind. Ports 8765/8766 are sandcastle-specific, so this is safe.
        if _port_open(comp.port):
            evicted = _evict_port(comp.port)
            if evicted:
                Console().print(
                    f"  [yellow]![/] evicted stale process on "
                    f":{comp.port} (PID {evicted})"
                )
        with _Spinner(comp.label) as sp:
            pid = runtime.start_component(workdir, comp, env)
            ok = runtime.wait_for_port("127.0.0.1", comp.port, timeout=15)
            if ok:
                sp.finish(f"ready on :{comp.port} (PID {pid})", ok=True)
            else:
                sp.finish(
                    f"started but :{comp.port} not yet listening (PID {pid})",
                    ok=False,
                )

    # 5. Castle + the one next action, both inside the same panel
    #    so the art doesn't get pushed off-screen by the call to
    #    action below it.
    _print_castle("Sandcastle Sim is up", next_url="http://localhost:8766")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Gracefully stop sim + MCP, then bring the Docker stack down.

    Each Sandcastle-managed background process gets SIGTERM and up
    to 10 s to shut down cleanly. If a process refuses to exit
    we escalate to SIGKILL and report that to the user. Docker
    Compose handles its own graceful shutdown of Mosquitto + HA
    (10 s grace before SIGKILL by default).
    """
    from . import runtime

    workdir = _resolve_workdir(args.workdir)
    print("Stopping Sandcastle Sim ...", flush=True)
    for comp in runtime.COMPONENTS:
        pid = runtime.status_component(workdir, comp)
        if pid is None:
            print(f"  • {comp.label}: not running", end="", flush=True)
        else:
            print(f"  • {comp.label}: SIGTERM -> PID {pid}", end="", flush=True)
            result = runtime.stop_component(
                workdir, comp, timeout=10.0, progress=True,
            )
            if result == "stopped":
                print(" stopped", end="", flush=True)
            elif result == "killed":
                print(" KILLED (didn't respond to SIGTERM)", end="", flush=True)
            else:
                print(f" {result}", end="", flush=True)
        # Evict anything still holding the port (orphaned process with a
        # stale/missing PID file that stop_component couldn't reach).
        if _port_open(comp.port):
            evicted = _evict_port(comp.port)
            if evicted:
                print(f" (evicted stale PID {evicted} on :{comp.port})", end="")
        print()

    if args.keep_docker:
        print("  • Mosquitto + Home Assistant: kept (--keep-docker)")
        _print_castle("Sandcastle Sim background processes stopped")
        return 0

    print("  • Mosquitto + Home Assistant: docker compose down ...",
          end="", flush=True)
    rc = subprocess.call(
        ["docker", "compose", "down"],
        cwd=str(workdir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL if args.quiet else None,
    )
    if rc != 0:
        print(" failed")
        return rc
    print(" stopped")
    _print_castle("Sandcastle Sim is down")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Tail the background components' log files."""
    from . import runtime

    workdir = _resolve_workdir(args.workdir)
    if args.component == "all":
        names = [c.name for c in runtime.COMPONENTS]
    elif args.component in {"ha", "homeassistant", "mosquitto", "docker"}:
        # Special case: tail Docker container logs instead.
        cmd = ["docker", "compose", "logs", "-f", "--tail=200"]
        if args.component in ("ha", "homeassistant"):
            cmd.append("homeassistant")
        elif args.component == "mosquitto":
            cmd.append("mosquitto")
        try:
            return subprocess.call(cmd, cwd=str(workdir))
        except KeyboardInterrupt:
            return 0
    else:
        names = [args.component]
    return runtime.tail_logs(workdir, names, follow=not args.no_follow, lines=args.lines)


def _run_bootstrap(workdir: Path, quiet: bool = False) -> int:
    """Run the HA bootstrap module as a subprocess so its cwd-based
    .env discovery picks up the right workdir. Idempotent."""
    kwargs = {"cwd": str(workdir)}
    if quiet:
        kwargs["stdout"] = subprocess.DEVNULL
    return subprocess.call(
        [sys.executable, "-m", "sandcastle_sim.bootstrap"], **kwargs,
    )


def _env_with_dotenv(workdir: Path) -> dict:
    """Return a copy of the environment with workdir/.env overlaid.

    Also sets ``SANDCASTLE_WORKDIR`` so subprocesses (sim, mcp, the
    topology loader, the floorplan resolver) can find the user's
    persisted home in ``<workdir>/.sandcastle/``.
    """
    env = os.environ.copy()
    env["SANDCASTLE_WORKDIR"] = str(workdir)
    env_path = workdir / ".env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def cmd_status(args: argparse.Namespace) -> int:
    """Print port reachability + background-process PIDs."""
    from rich.console import Console
    from rich.table import Table

    from . import runtime

    workdir = _resolve_workdir(getattr(args, "workdir", None))
    checks = [
        ("Mosquitto",      1883, None),
        ("Home Assistant", 8123, None),
        ("MCP server",     8765, "mcp"),
        ("Simulator+GUI",  8766, "sim"),
        ("Ollama",         11434, None),
    ]
    comps_by_name = {c.name: c for c in runtime.COMPONENTS}

    table = Table(
        title="Sandcastle Sim — status",
        title_style="bold",
        title_justify="left",
        header_style="bold cyan",
        box=None,
        padding=(0, 2),
    )
    table.add_column("State", justify="left", no_wrap=True)
    table.add_column("Component", style="bold")
    table.add_column("Endpoint", style="dim")
    table.add_column("Owned by", style="dim")

    for label, port, comp_name in checks:
        on = _port_open(port)
        state = "[green]●[/green] UP" if on else "[red]○[/red] DOWN"
        owner = ""
        if comp_name:
            comp = comps_by_name.get(comp_name)
            if comp:
                pid = runtime.status_component(workdir, comp)
                if pid:
                    owner = f"sandcastle (PID {pid})"
                elif on:
                    owner = "foreground or external"
        table.add_row(state, label, f"127.0.0.1:{port}", owner)

    console = Console()
    console.print(table)
    console.print(f"\n[dim]Workdir:[/] {workdir}")
    return 0


def cmd_floorplan(args: argparse.Namespace) -> int:
    """Floor-plan layout commands. ``floorplan auto`` is the only one today."""
    subcmd = getattr(args, "floorplan_cmd", None)
    if subcmd == "auto":
        return _floorplan_auto(args)
    print("Usage: sandcastle-sim floorplan auto [--force]")
    return 1


# Maps HA domain (+ device_class for sensors) to the floor-plan `type`
# string the GUI's renderers understand. Anything unmapped becomes
# `default` (still placed, but rendered with the generic renderer).
def _floorplan_type(device: dict) -> Optional[str]:
    domain = device.get("domain") or ""
    attrs = device.get("attributes") or {}
    dc = attrs.get("device_class")
    if domain == "light":     return "light"
    if domain == "switch":    return "switch"
    if domain == "lock":      return "lock"
    if domain == "cover":     return "cover"
    if domain == "climate":   return "climate"
    if domain == "vacuum":    return "vacuum"
    if domain == "sensor":
        if dc == "temperature": return "temp"
        if dc == "power":       return "power"
        return None
    if domain == "binary_sensor":
        if dc == "motion":               return "motion"
        if dc in ("door", "window", "opening"): return "contact"
        if dc == "moisture":             return "leak"
        if dc == "smoke":                return "smoke"
        return None
    return None


def _load_dotenv_into(env: dict, workdir: Path) -> None:
    """Mirror cmd_mcp's .env loader. Idempotent — won't clobber existing vars."""
    path = workdir / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _floorplan_auto(args: argparse.Namespace) -> int:
    """Re-layout the floor plan from the live HA inventory.

    Connects to the MCP server for area/domain resolution AND to HA's
    REST `/api/states` for full per-entity attributes (the MCP path
    strips `device_class`, which we need to map binary_sensor → motion
    / contact / leak / smoke). Result is run through `auto_layout`
    and written back.

    Default behaviour preserves coordinates for entities already in
    the file; `--force` re-places everything from scratch.
    """
    import asyncio
    import json as _json

    from . import floorplan as fp

    mcp_url = args.mcp_url
    force = bool(getattr(args, "force", False))

    # Load .env so HA_TOKEN / HA_URL are populated. Same pattern as
    # cmd_mcp — keeps the surface consistent.
    _load_dotenv_into(os.environ, Path.cwd())

    # Source-of-truth: workdir copy if seeded, else the bundled seed.
    # `floorplan auto` writes to the workdir copy so the package is
    # never mutated. --out only redirects the *write* — useful for
    # dry-running without touching the live floor plan.
    workdir = _resolve_workdir(getattr(args, "workdir", None))
    canonical = fp.resolve_floorplan_path(workdir)
    if not canonical.exists():
        print(f"floorplan.json not found at {canonical}", file=sys.stderr)
        return 2

    out_arg = getattr(args, "out", None)
    if out_arg:
        target = Path(out_arg)
    else:
        # Always write to workdir's state dir — never the package seed.
        target = workdir / ".sandcastle" / "floorplan.json"
        target.parent.mkdir(parents=True, exist_ok=True)

    try:
        existing = fp.load_floorplan(canonical)
    except fp.FloorplanError as exc:
        print(f"existing {canonical} is invalid: {exc}", file=sys.stderr)
        return 2

    rooms = existing.get("rooms", {})
    existing_devices = existing.get("devices", {})

    # Pull inventory from the MCP server.
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:
        print(
            "The `mcp` package is required for `floorplan auto`. "
            "Install it via `pip install mcp`.",
            file=sys.stderr,
        )
        return 2

    import aiohttp

    ha_url = os.environ.get("HA_URL", "http://localhost:8123").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")

    async def _fetch_inventory() -> List[dict]:
        # MCP gives us area + domain + entity_id (resolved against HA's
        # device registry). It strips `device_class` to keep the agent
        # payload light, so we hit HA REST /api/states in parallel and
        # merge full attributes back in.
        async def _mcp_devices() -> List[dict]:
            async with streamablehttp_client(mcp_url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool("list_devices", {})
                    blocks = getattr(res, "content", None) or []
                    if not blocks:
                        return []
                    txt = getattr(blocks[0], "text", None) or "{}"
                    return _json.loads(txt).get("devices", []) or []

        async def _ha_states() -> Dict[str, dict]:
            if not ha_token:
                return {}
            headers = {"Authorization": f"Bearer {ha_token}"}
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(f"{ha_url}/api/states") as r:
                    r.raise_for_status()
                    states = await r.json()
            return {s["entity_id"]: s for s in states}

        devices, states = await asyncio.gather(_mcp_devices(), _ha_states())
        # Merge full attributes (including device_class) over the
        # stripped MCP attrs.
        for d in devices:
            st = states.get(d.get("entity_id"))
            if not st:
                continue
            full_attrs = (st.get("attributes") or {})
            d["attributes"] = {**(d.get("attributes") or {}), **full_attrs}
        return devices

    try:
        devices = asyncio.run(_fetch_inventory())
    except Exception as exc:
        print(
            f"Could not reach the stack: {exc}\n"
            f"Is everything running? Try `sandcastle-sim status`.",
            file=sys.stderr,
        )
        return 2

    # Shape the inventory for auto_layout: keep only entities we know
    # how to place. Skipped entities are listed at the end.
    inventory: List[dict] = []
    skipped: List[str] = []
    for d in devices:
        layout_type = _floorplan_type(d)
        if layout_type is None:
            skipped.append(d.get("entity_id", "?"))
            continue
        inventory.append({
            "entity_id": d["entity_id"],
            "area": d.get("area"),
            "type": layout_type,
        })

    new_devices = fp.auto_layout(
        inventory, rooms, existing=existing_devices, force=force,
    )

    new_data = dict(existing)
    new_data["devices"] = new_devices

    try:
        fp.save_floorplan(target, new_data)
    except fp.FloorplanError as exc:
        print(f"refusing to write invalid floorplan: {exc}", file=sys.stderr)
        return 2

    placed = len(new_devices)
    kept = sum(
        1 for eid, dev in new_devices.items()
        if eid in existing_devices and not force
    )
    print(f"Wrote {target}")
    print(f"  {placed} devices placed ({kept} kept from existing, "
          f"{placed - kept} (re-)laid out)")
    if skipped:
        print(f"  {len(skipped)} devices skipped (no floor-plan type):")
        for s in skipped[:10]:
            print(f"    - {s}")
        if len(skipped) > 10:
            print(f"    ... and {len(skipped) - 10} more")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Run a YAML eval suite against the live agent + stack.

    Three workflows:

      * ``sandcastle-sim eval`` — run + report, no persistence.
      * ``sandcastle-sim eval --save-baseline`` — run + write the
        results to ``.sandcastle/eval-baseline.json``. Do this
        BEFORE making changes.
      * ``sandcastle-sim eval --diff`` — run + diff against the
        saved baseline. Do this AFTER making changes. Exit code
        non-zero on any regression so coding agents can detect
        "my changes broke something" automatically.
    """
    import asyncio
    from pathlib import Path

    from .evals import (
        default_baseline_path,
        diff_runs,
        has_regressions,
        load_run,
        load_suite,
        print_diff_report,
        print_summary,
        run_suite,
        save_run,
    )

    if not _port_open(8765):
        print(
            "MCP server isn't running on :8765.\n"
            "Start it with: sandcastle-sim start  (or sandcastle-sim mcp)",
            file=sys.stderr,
        )
        return 1
    if not _port_open(11434):
        print(
            f"Ollama isn't running on :11434.\n"
            f"Install Ollama (https://ollama.com), then:\n"
            f"  ollama pull {args.model}\n"
            f"  ollama serve",
            file=sys.stderr,
        )
        return 1

    # Resolve suite path. Default search order: explicit --suite,
    # then evals/quick.yaml in the current dir, then the bundled
    # quick.yaml from the package.
    if args.suite:
        suite_path = Path(args.suite)
    else:
        cwd_quick = Path.cwd() / "evals" / "quick.yaml"
        if cwd_quick.is_file():
            suite_path = cwd_quick
        else:
            from importlib import resources
            try:
                bundled = resources.files("sandcastle_sim").joinpath(
                    "data", "evals", "quick.yaml",
                )
                suite_path = Path(str(bundled))
            except Exception:
                suite_path = cwd_quick

    if not suite_path.is_file():
        print(
            f"eval suite not found: {suite_path}\n"
            f"Pass --suite path/to/suite.yaml, or run from a checkout of the repo.",
            file=sys.stderr,
        )
        return 1

    cases = load_suite(suite_path)
    if not cases:
        print(f"{suite_path}: no cases found", file=sys.stderr)
        return 1

    # Resolve the baseline path — explicit override or default in
    # the workdir's .sandcastle/.
    workdir = _resolve_workdir(getattr(args, "workdir", None))
    baseline_path = (
        Path(args.baseline_path) if args.baseline_path
        else default_baseline_path(workdir)
    )

    warmup_s, results = asyncio.run(
        run_suite(
            cases,
            mcp_url=args.mcp_url,
            ollama_url=args.ollama_url,
            model=args.model,
            max_iterations=args.max_iterations,
            repeat=max(1, int(args.repeat)),
            live=True,
            suite_name=suite_path.name,
            **_optimization_overrides(args),
        )
    )

    if args.save_baseline:
        save_run(results, baseline_path, warmup_s=warmup_s)
        print_summary(results)
        print(f"\n  baseline saved to {baseline_path}")
        return 0

    if args.diff:
        saved_at, _baseline_warmup_s, baseline = load_run(baseline_path)
        if not baseline:
            print(
                f"no baseline found at {baseline_path}\n"
                f"Save one first with: sandcastle-sim eval --save-baseline",
                file=sys.stderr,
            )
            print_summary(results)
            return 1
        entries = diff_runs(baseline, results)
        print_diff_report(entries, saved_at=saved_at, suite_name=suite_path.name)
        return 1 if has_regressions(entries) else 0

    print_summary(results)
    failed = [r for r in results if not r.passed]
    return 0 if not failed else 1


def cmd_chat(args: argparse.Namespace) -> int:
    """Open an interactive chat REPL against the MCP server.

    Reuses the same OneShotAgent machinery as the ``agent`` /
    bare-prompt commands but keeps the MCP and Ollama connections
    alive across many prompts so the model stays warm and the SSE
    feed keeps streaming. ``/help`` inside the REPL shows sample
    prompts and slash commands.
    """
    from .agent import run_chat_sync

    if not _port_open(8765):
        print(
            "MCP server isn't running on :8765.\n"
            "Start it with: sandcastle-sim start  (or sandcastle-sim mcp)",
            file=sys.stderr,
        )
        return 1
    if not _port_open(11434):
        print(
            f"Ollama isn't running on :11434.\n"
            f"Install Ollama (https://ollama.com), then:\n"
            f"  ollama pull {args.model}\n"
            f"  ollama serve",
            file=sys.stderr,
        )
        return 1

    return run_chat_sync(
        mcp_url=args.mcp_url,
        ollama_url=args.ollama_url,
        model=args.model,
        max_iterations=args.max_iterations,
        quiet=args.quiet,
        show_raw=args.verbose,
        **_optimization_overrides(args),
    )


def cmd_agent(args: argparse.Namespace) -> int:
    """Run a single prompt through the built-in Ollama + MCP agent.

    The CLI agent is a minimal reference, not a production tool.
    See docs/integrating-your-agent.md for connecting your own
    agent to the same MCP surface.
    """
    from .agent import run_one_shot

    if not args.prompt:
        print("error: agent requires a prompt", file=sys.stderr)
        return 2
    prompt = " ".join(args.prompt)

    if not _port_open(8765):
        print(
            "MCP server isn't running on :8765.\n"
            "Start it in another terminal: sandcastle-sim mcp",
            file=sys.stderr,
        )
        return 1
    if not _port_open(11434):
        print(
            f"Ollama isn't running on :11434.\n"
            f"Install Ollama (https://ollama.com), then:\n"
            f"  ollama pull {args.model}\n"
            f"  ollama serve",
            file=sys.stderr,
        )
        return 1

    print(f"> {prompt}", flush=True)
    result = run_one_shot(
        prompt,
        mcp_url=args.mcp_url,
        ollama_url=args.ollama_url,
        model=args.model,
        max_iterations=args.max_iterations,
        quiet=args.quiet,
        show_raw=args.verbose,
        **_optimization_overrides(args),
    )
    if result.error:
        print(f"\nerror: {result.error}", file=sys.stderr)
        return 1
    if not result.response_already_printed:
        print()
        print(result.response_text)
    if args.verbose:
        print(
            f"\n(used {result.iterations} iteration(s), "
            f"{len(result.tool_calls)} tool call(s))",
            file=sys.stderr,
        )
    return 0


# --------------------------------------------------------------------------- #
# Argparse plumbing                                                           #
# --------------------------------------------------------------------------- #


def _optimization_overrides(args: argparse.Namespace) -> dict:
    """Turn ``--no-routing`` / ``--no-cheat-sheet`` flags into the
    OneShotAgent kwargs that override the env-var-driven defaults.

    Per-invocation flags are explicit and can't leak — that's why
    the README walkthrough uses these instead of the env vars.
    Only set kwargs the user actually requested so we don't
    override explicit env-var-set defaults from elsewhere.
    """
    overrides: dict = {}
    if getattr(args, "no_routing", False):
        overrides["route_tools"] = False
    if getattr(args, "no_cheat_sheet", False):
        overrides["inject_cheat_sheet"] = False
    return overrides


def _add_workdir_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--workdir",
        default=None,
        help=(
            "Where to find / create docker-compose.yml + ha-config/. "
            "Defaults to cwd if it looks like a repo checkout, else "
            "~/.local/share/sandcastle-sim."
        ),
    )


def _add_agent_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--mcp-url", default="http://localhost:8765/mcp/",
        help="Sandcastle MCP server URL (default: %(default)s)",
    )
    p.add_argument(
        "--ollama-url", default="http://localhost:11434",
        help="Ollama base URL (default: %(default)s)",
    )
    p.add_argument(
        "--model", default=os.environ.get("SANDCASTLE_MODEL", "gemma4:e4b"),
        help=(
            "Ollama model id (default: %(default)s; override via "
            "SANDCASTLE_MODEL). Pull with: ollama pull gemma4:e4b"
        ),
    )
    p.add_argument(
        "--max-iterations", type=int, default=8,
        help="Max LLM iterations per turn (default: %(default)s)",
    )
    p.add_argument(
        "-q", "--quiet", action="store_true",
        help="Don't print the live tool-call trace",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help=(
            "Dump raw model output per iteration (content, tool_calls, "
            "thinking, eval timings) to stderr, plus iteration / tool "
            "count summary at the end."
        ),
    )
    p.add_argument(
        "--no-routing",
        action="store_true",
        help=(
            "Disable the keyword tool router for this run (sends all "
            "MCP tools to the model). Equivalent to "
            "SANDCASTLE_DISABLE_ROUTING=1, but flag-scoped — won't "
            "leak into your next invocation. Used in the README's "
            "eval walkthrough to demonstrate the diff harness."
        ),
    )
    p.add_argument(
        "--no-cheat-sheet",
        action="store_true",
        help=(
            "Disable the device cheat-sheet preload for this run "
            "(model has to discover devices itself). Heavier hit "
            "than --no-routing; useful for benchmarking the "
            "un-helped model."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandcastle-sim",
        description=(
            "Smart-home sandbox for AI agents. Bring up Mosquitto + Home "
            "Assistant + a 23-device simulator + an MCP server, and either "
            "drive it from your own MCP client or use the built-in Ollama "
            "agent: sandcastle-sim 'turn off the kitchen light'."
        ),
    )
    parser.add_argument(
        "--version", action="version",
        version=_version_string(),
    )
    sub = parser.add_subparsers(dest="cmd")

    # The headline command. Bundles the entire bring-up so first-run
    # users don't juggle four separate commands.
    p_start = sub.add_parser(
        "start",
        help=(
            "One-shot: bring up Docker + bootstrap HA + spawn the "
            "simulator and MCP server in the background. Open the "
            "floor plan in a browser when it returns."
        ),
    )
    _add_workdir_arg(p_start)
    p_start.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress docker / bootstrap progress output",
    )
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser(
        "stop",
        help="Stop the simulator + MCP, then bring the Docker stack down.",
    )
    _add_workdir_arg(p_stop)
    p_stop.add_argument(
        "--keep-docker", action="store_true",
        help="Stop simulator + MCP only; leave Mosquitto + HA running.",
    )
    p_stop.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress docker compose down output",
    )
    p_stop.set_defaults(func=cmd_stop)

    p_logs = sub.add_parser(
        "logs",
        help=(
            "Tail logs. `sandcastle-sim logs sim` / `mcp` / `all` for the "
            "background processes; `ha` / `mosquitto` / `docker` for "
            "container logs."
        ),
    )
    _add_workdir_arg(p_logs)
    p_logs.add_argument(
        "component", nargs="?", default="all",
        help="sim | mcp | all | ha | mosquitto | docker (default: all)",
    )
    p_logs.add_argument("--no-follow", action="store_true")
    p_logs.add_argument("--lines", type=int, default=50)
    p_logs.set_defaults(func=cmd_logs)

    p_up = sub.add_parser(
        "up",
        help="Start Mosquitto + Home Assistant only. Use `start` for the full bring-up.",
    )
    _add_workdir_arg(p_up)
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser(
        "down",
        help="Stop the Docker stack only. Use `stop` for the full tear-down.",
    )
    _add_workdir_arg(p_down)
    p_down.set_defaults(func=cmd_down)

    p_boot = sub.add_parser(
        "bootstrap",
        help="Onboard HA, mint long-lived token, set up MQTT (idempotent).",
    )
    _add_workdir_arg(p_boot)
    p_boot.set_defaults(func=cmd_bootstrap)

    p_reset = sub.add_parser(
        "reset",
        help=(
            "Wipe HA's persisted state (DB, .storage, onboarding, .env) "
            "so the next start is a clean bootstrap. Tracked configs "
            "(configuration.yaml, scenes.yaml) are preserved."
        ),
    )
    p_reset.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt.",
    )
    _add_workdir_arg(p_reset)
    p_reset.set_defaults(func=cmd_reset)

    p_sim = sub.add_parser(
        "sim", help="Run the device simulator + floor-plan GUI (foreground).",
    )
    _add_workdir_arg(p_sim)
    p_sim.set_defaults(func=cmd_sim)

    p_mcp = sub.add_parser(
        "mcp", help="Run the smart-home MCP server (foreground).",
    )
    _add_workdir_arg(p_mcp)
    p_mcp.set_defaults(func=cmd_mcp)

    p_status = sub.add_parser(
        "status", help="Show which Sandcastle ports are reachable.",
    )
    p_status.set_defaults(func=cmd_status)

    p_agent = sub.add_parser(
        "agent",
        help=(
            "Run a single prompt through the built-in Ollama + MCP agent. "
            "Same as the bare invocation: `sandcastle-sim \"prompt\"`."
        ),
    )
    p_agent.add_argument("prompt", nargs="*", help="Natural-language prompt")
    _add_agent_args(p_agent)
    p_agent.set_defaults(func=cmd_agent)

    p_chat = sub.add_parser(
        "chat",
        help=(
            "Interactive REPL: keep typing prompts, like `ollama run`. "
            "Connections stay open so the model stays warm; type /help "
            "inside for sample prompts and slash commands."
        ),
    )
    _add_agent_args(p_chat)
    p_chat.set_defaults(func=cmd_chat)

    p_floorplan = sub.add_parser(
        "floorplan",
        help=(
            "Floor-plan layout helpers. `floorplan auto` re-lays out "
            "the GUI's device positions from the live HA inventory."
        ),
    )
    p_floorplan_sub = p_floorplan.add_subparsers(dest="floorplan_cmd")
    p_floorplan_auto = p_floorplan_sub.add_parser(
        "auto",
        help=(
            "Place devices on the floor plan deterministically based on "
            "device type. Reads inventory from the MCP server. By default "
            "preserves coordinates for devices already laid out — pass "
            "--force to re-lay-out everything."
        ),
    )
    p_floorplan_auto.add_argument(
        "--force", action="store_true",
        help="Re-place every device, ignoring existing coordinates.",
    )
    p_floorplan_auto.add_argument(
        "--mcp-url", default="http://localhost:8765/mcp/",
        help="MCP server URL (default: http://localhost:8765/mcp/).",
    )
    p_floorplan_auto.add_argument(
        "--out", default=None,
        help="Override the floorplan.json output path (default: <workdir>/floorplan.json).",
    )
    _add_workdir_arg(p_floorplan_auto)
    p_floorplan.set_defaults(func=cmd_floorplan)

    p_eval = sub.add_parser(
        "eval",
        help=(
            "Run a YAML eval suite against the live agent. Regression "
            "net for your agent — write down what it should do, re-run "
            "every time you change the agent. Defaults to the bundled "
            "quick suite."
        ),
    )
    p_eval.add_argument(
        "--suite",
        default=None,
        help="Path to a YAML eval file (default: evals/quick.yaml from the repo or bundled).",
    )
    p_eval.add_argument(
        "--save-baseline",
        action="store_true",
        help=(
            "Run + save results to .sandcastle/eval-baseline.json. Do this "
            "BEFORE making changes; afterwards use --diff to see what changed."
        ),
    )
    p_eval.add_argument(
        "--diff",
        action="store_true",
        help=(
            "Run + diff against the saved baseline. Highlights regressions, "
            "progressions, latency changes. Exit code non-zero on any "
            "regression so coding agents can detect breakage automatically."
        ),
    )
    p_eval.add_argument(
        "--baseline-path",
        default=None,
        help=(
            "Override the default baseline location "
            "(.sandcastle/eval-baseline.json in the workdir)."
        ),
    )
    p_eval.add_argument(
        "--repeat",
        type=int,
        default=3,
        help=(
            "Run each case N times and report the median latency "
            "(default 3). Pass requires all N to pass — any flake "
            "counts as a regression. Warmup is paid once regardless "
            "of N because the model stays pinned, so the marginal "
            "cost is just N × per-case time. Use --repeat 1 for fast "
            "iteration when you don't care about steady-state numbers."
        ),
    )
    _add_workdir_arg(p_eval)
    _add_agent_args(p_eval)
    p_eval.set_defaults(func=cmd_eval)

    return parser


def _version_string() -> str:
    try:
        from importlib.metadata import version
        return f"sandcastle-sim {version('sandcastle-sim')}"
    except Exception:
        from . import __version__
        return f"sandcastle-sim {__version__}"


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Bare-invocation shorthand: if the first arg isn't a known
    # subcommand AND it doesn't start with "-", treat the whole tail
    # as a prompt for the agent. This makes the headline use case
    # (`sandcastle-sim "turn off the kitchen light"`) work cleanly.
    parser = build_parser()
    known_cmds = {
        "start", "stop", "logs",
        "up", "down", "bootstrap", "reset", "sim", "mcp", "status", "agent", "chat",
        "eval", "floorplan",
    }
    if argv and not argv[0].startswith("-") and argv[0] not in known_cmds:
        argv = ["agent", *argv]

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
