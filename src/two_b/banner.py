"""Startup banner: a small glowing moon + header, shown once when 2B launches.

Brand vs. terminal: the parchment/olive YoRHa-menu palette is 2B's identity for
the README and web, but a real terminal has the user's own (usually dark)
background, where dark-olive ink would be invisible. So the terminal banner
renders light-on-dark — a cream/bronze moon that glows on the terminal ground —
which is the right setting for a moon anyway. Shown only in interactive mode.
"""
import os

from rich.console import Console
from rich.text import Text

from . import __version__

CREAM = "rgb(207,200,175)"
BRONZE = "rgb(190,160,95)"
DIM = "rgb(140,135,116)"
FAINT = "rgb(105,101,84)"


def _abbrev_cwd(cwd: str) -> str:
    home = os.path.expanduser("~")
    return "~" + cwd[len(home):] if cwd.startswith(home) else cwd


def render(console: Console, model: str, cwd: str) -> None:
    name = Text()
    name.append("2B", style=f"bold {CREAM}")
    name.append(f"  v{__version__}", style=DIM)

    meta = Text()
    meta.append(model, style=BRONZE)
    meta.append("  ·  local  ·  Ollama", style=DIM)

    path = Text(_abbrev_cwd(cwd), style=FAINT)

    notice1 = Text()
    notice1.append("▌ ", style=BRONZE)
    notice1.append("Local models, kept on task.", style=CREAM)
    notice2 = Text()
    notice2.append("▌ ", style=BRONZE)
    notice2.append("Type a task to begin, or ", style=DIM)
    notice2.append("/", style=BRONZE)
    notice2.append(" for commands.", style=DIM)

    foot = Text("  / commands   ·   ctrl+b background   ·   ctrl+d quit", style=FAINT)

    console.print()
    console.print(name)
    console.print(meta)
    console.print(path)
    console.print()
    console.print(notice1)
    console.print(notice2)
    console.print()
    console.print(foot)
