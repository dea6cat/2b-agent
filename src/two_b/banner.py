"""Startup banner: a small glowing moon + header, shown once when 2B launches.

Brand vs. terminal: the parchment/olive YoRHa-menu palette is 2B's identity for
the README and web, but a real terminal has the user's own (usually dark)
background, where dark-olive ink would be invisible. So the terminal banner
renders light-on-dark — a cream/bronze moon that glows on the terminal ground —
which is the right setting for a moon anyway. Shown only in interactive mode.
"""
import os

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from . import __version__

CREAM = "rgb(207,200,175)"
BRONZE = "rgb(190,160,95)"
DIM = "rgb(140,135,116)"
FAINT = "rgb(105,101,84)"
SHADOW = "rgb(74,70,54)"

# Waning gibbous: mostly lit (L), thin shadow sliver on the right (S). Each cell
# is drawn two blocks wide so the disc reads round in a terminal's tall cells.
_MOON = [
    " LLL ",
    "LLLLS",
    "LLLLS",
    "LLLLS",
    " LLL ",
]


def _moon_lines() -> list[Text]:
    lines = []
    for row in _MOON:
        t = Text()
        for c in row:
            if c == "L":
                t.append("██", style=CREAM)
            elif c == "S":
                t.append("██", style=SHADOW)
            else:
                t.append("  ")
        lines.append(t)
    return lines


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

    info = Group(Text(""), name, meta, path, Text(""))

    header = Table.grid(padding=(0, 3))
    header.add_column()
    header.add_column()
    header.add_row(Group(*_moon_lines()), info)

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
    console.print(header)
    console.print()
    console.print(notice1)
    console.print(notice2)
    console.print()
    console.print(foot)
