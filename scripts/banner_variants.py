"""Render candidate castle banners side-by-side for brainstorming.

Run this in your real terminal — that's the only place you see how
the colors and proportions actually feel:

    python scripts/banner_variants.py

Each variant prints with a heading. Pick one (or steal pieces) and
we update _ascii_castle in cli.py.

Mix of raw-ANSI and Rich-based variants so you can compare. Rich
gives easy gradients, panels, and Unicode rendering; raw ANSI is
the dependency-free baseline.
"""

from __future__ import annotations

import sys

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()


def heading(name: str) -> None:
    console.rule(f"[bold cyan]{name}[/]", align="left")


# --------------------------------------------------------------------------- #
# 1. Current — three towers + pennants + waves (raw ANSI baseline)            #
# --------------------------------------------------------------------------- #

CURRENT = r"""
        |>>>             |>>>             |>>>
        |                |                |
   _____|_____      _____|_____      _____|_____
  |   . ' .   |    |  .   ` .  |    |   . ` .   |
  |  ___ ___  |____|  __     _ |____|  ___ ___  |
  | |   |   | |    | |  |   |   |    | |   |   | |
  |_|___|___|_|____|_|__|___|___|____|_|___|___|_|
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~"""


def variant_current() -> None:
    heading("1. Current — three towers, pennants, waves")
    console.print(f"[cyan]{CURRENT}[/]")
    console.print("[bold]  Sandcastle Sim is up[/]\n")


# --------------------------------------------------------------------------- #
# 2. Single tall keep — more iconic, more detail                              #
# --------------------------------------------------------------------------- #

TALL_KEEP = r"""
                  |>>>>
                  |
              ____|____
             /         \
            /  M  M  M  \
           |   _ _ _ _   |
        ___|__| | | |__|_|___
       |   _   _ ___ _   _   |
       |  | | | |   | | | |  |
       |  | | | |   | | | |  |
       |  | | | | _ | | | |  |
       |__|_|_|_||_||_|_|_|__|
       |_|_|_|_|     |_|_|_|_|
   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~"""


def variant_tall_keep() -> None:
    heading("2. Single tall keep — windows, gate, banner")
    console.print(f"[gold1]{TALL_KEEP}[/]")
    console.print("[bold]  Sandcastle Sim is up[/]\n")


# --------------------------------------------------------------------------- #
# 3. Beach scene — sun + bird + castle + waves                                #
# --------------------------------------------------------------------------- #

BEACH = r"""
       *  .  ✦      ___      ✦  .   .
      .    *      _( o )_      *
                .  \\___/  .          ~  ~  ~
            ___           ___
           |_|_|_._._._._|_|_|
           |  |.|       |.|  |
           |__|_|_______|_|__|
   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~"""


def variant_beach() -> None:
    heading("3. Beach scene — sun + sky + castle + ocean")
    txt = Text(BEACH)
    # Recolor each line by what it represents — sky / sun / castle / sea.
    lines = BEACH.splitlines()
    out = Text()
    for line in lines:
        if "*" in line or "✦" in line:
            out.append(line + "\n", style="bright_yellow")
        elif "~" in line:
            out.append(line + "\n", style="cyan")
        elif "_" in line or "|" in line:
            out.append(line + "\n", style="gold1")
        else:
            out.append(line + "\n")
    console.print(out)
    console.print("[bold]  Sandcastle Sim is up[/]\n")


# --------------------------------------------------------------------------- #
# 4. Mini signature — small castle inline with the message                    #
# --------------------------------------------------------------------------- #


def variant_mini_inline() -> None:
    heading("4. Mini signature — castle inline with the message")
    art = "[gold1]/¯|_/¯¯|_/¯|[/]"
    console.print(f"  {art}  [bold]Sandcastle Sim is up[/]")
    console.print(f"  [cyan]~~~~~~~~~~~~~[/]\n")


# --------------------------------------------------------------------------- #
# 5. Rich Panel — bordered title with castle inside                           #
# --------------------------------------------------------------------------- #


def variant_panel() -> None:
    heading("5. Rich Panel — castle inside a bordered box")
    body = Text()
    body.append(CURRENT.lstrip("\n"), style="gold1")
    body.append("\n\n  Sandcastle Sim is up", style="bold")
    panel = Panel(
        Align.left(body),
        title="[bold cyan]Sandcastle Sim[/]",
        border_style="cyan",
        padding=(1, 2),
    )
    console.print(panel)


# --------------------------------------------------------------------------- #
# 6. Whimsical — castle with bucket / shovel / footprints                     #
# --------------------------------------------------------------------------- #

WHIMSY = r"""
                            ___
                           [___]            *
              |>>          |   |
          ____|___        / ⌒ \         .
         |/\/\/\/\|      |  o  |
         |  ___   |       \___/
         | |   |  |         ||
         |_|___|__|_________||_______
         ~~~~~~~~~~~~~~~~~~~~~~~~~~~
              .  o   .          .  o   .
                .       .   .        .
"""


def variant_whimsy() -> None:
    heading("6. Whimsical — bucket, shovel, footprints in sand")
    lines = WHIMSY.splitlines()
    out = Text()
    for line in lines:
        if "~" in line:
            out.append(line + "\n", style="cyan")
        elif "*" in line:
            out.append(line + "\n", style="bright_yellow")
        elif " o " in line or "  ." in line:
            out.append(line + "\n", style="dim yellow")
        else:
            out.append(line + "\n", style="gold1")
    console.print(out)
    console.print("[bold]  Sandcastle Sim is up[/]\n")


# --------------------------------------------------------------------------- #
# 7. Two-color gradient — sky → castle → sea                                  #
# --------------------------------------------------------------------------- #


def variant_gradient() -> None:
    heading("7. Two-color gradient — sky / castle / sea")
    sky = "       . *   .    *   . ✦      *   .   ✦   .   *"
    castle_lines = CURRENT.lstrip("\n").splitlines()
    out = Text()
    out.append(sky + "\n", style="bright_blue")
    for line in castle_lines[:-1]:
        out.append(line + "\n", style="gold1")
    out.append(castle_lines[-1] + "\n", style="cyan")
    console.print(out)
    console.print("[bold]  Sandcastle Sim is up[/]\n")


# --------------------------------------------------------------------------- #


def main() -> None:
    if "--list" in sys.argv:
        for name in sorted(globals()):
            if name.startswith("variant_"):
                print(name.removeprefix("variant_"))
        return

    variants = [
        variant_current,
        variant_tall_keep,
        variant_beach,
        variant_mini_inline,
        variant_panel,
        variant_whimsy,
        variant_gradient,
    ]
    for fn in variants:
        fn()
    console.rule("[dim]End. Pick a number and tell the agent.[/]", align="left")


if __name__ == "__main__":
    main()
