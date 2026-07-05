"""`2b --rm` — uninstall 2B and delete its config, leaving no 2B-specific trace.

Removes the two things that are genuinely 2B's: the ~/.config/2b directory (API keys,
prefs, MCP config) and the installed executable, uninstalled via whatever installed it —
uv tool / pipx / pip (detected from where the files live). It deliberately
does NOT touch Ollama models (they're Ollama's — often large and shared) or your shell
PATH line, and it can't chase down 2B.md files scattered across your projects — it tells
you how to remove those yourself. Destructive: confirms unless `assume_yes`.

Kept out of cli.py (which imports prompt_toolkit at load) so it's importable and testable.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

from . import config
from .update import PKG, _install_kind


def run(emit, confirm, assume_yes: bool = False) -> int:
    """Show what will be removed, confirm (unless assume_yes), then remove it. `emit`
    is a print callable (rich markup ok); `confirm` is a callable(prompt)->bool.
    Returns 0 when removal ran, 1 when the user aborted."""
    cfg = config.CONFIG_DIR
    cfg_exists = cfg.exists()

    # how to uninstall the executable, chosen by how 2b was installed
    kind = _install_kind()
    if kind == "uv":
        tool, argv, shown = "uv", ["uv", "tool", "uninstall", PKG], f"uv tool uninstall {PKG}"
    elif kind == "pipx":
        tool, argv, shown = "pipx", ["pipx", "uninstall", PKG], f"pipx uninstall {PKG}"
    else:
        tool, argv, shown = "pip", [sys.executable, "-m", "pip", "uninstall", "-y", PKG], f"pip uninstall {PKG}"

    emit("[bold]2b --rm[/bold] will remove:")
    emit(f"  • config directory {cfg}"
         f"{'' if cfg_exists else ' (not present)'} — API keys, prefs, MCP config")
    emit(f"  • the installed 2b executable (via '{shown}')")
    emit("It will NOT touch: Ollama models (remove with [bold]ollama rm <model>[/bold]), "
         "any 2B.md files in your projects, or your shell PATH line.")

    if not assume_yes and not confirm("Permanently remove 2B and its config?"):
        emit("Aborted — nothing was removed.")
        return 1

    # 1) config directory
    if cfg_exists:
        try:
            shutil.rmtree(cfg)
            emit(f"[green]✓[/green] removed {cfg}")
        except OSError as e:
            emit(f"[red]✗[/red] could not remove {cfg}: {e}")
    else:
        emit(f"    {cfg} already absent")

    # 2) the executable — last, since removing it deletes files backing this running process
    #    (safe: the interpreter is already loaded; it just can't be re-run afterward). pip runs
    #    through the live interpreter (always available); uv/pipx must be on PATH.
    available = True if tool == "pip" else bool(shutil.which(tool))
    if available:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                emit("[green]✓[/green] uninstalled the 2b executable")
            else:
                emit(f"[yellow]![/yellow] '{shown}' said: {(r.stderr or r.stdout).strip()[:200]}")
                emit("    if 2b was installed another way, remove it with that tool.")
        except Exception as e:
            emit(f"[yellow]![/yellow] could not run {tool} uninstall ({e}) — remove 2b manually.")
    else:
        emit(f"[yellow]![/yellow] {tool} not found — remove the executable yourself "
             f"(e.g. [bold]{shown}[/bold]).")

    emit("[green]Done. 2B has been removed.[/green]")
    return 0
