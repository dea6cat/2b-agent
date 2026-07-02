"""`2b --rm` — uninstall 2B and delete its config, leaving no 2B-specific trace.

Removes the two things that are genuinely 2B's: the ~/.config/2b directory (API keys,
prefs, MCP config) and the installed executable (`uv tool uninstall 2b`). It deliberately
does NOT touch Ollama models (they're Ollama's — often large and shared) or your shell
PATH line, and it can't chase down 2B.md files scattered across your projects — it tells
you how to remove those yourself. Destructive: confirms unless `assume_yes`.

Kept out of cli.py (which imports prompt_toolkit at load) so it's importable and testable.
"""
from __future__ import annotations

import shutil
import subprocess

from . import config


def run(emit, confirm, assume_yes: bool = False) -> int:
    """Show what will be removed, confirm (unless assume_yes), then remove it. `emit`
    is a print callable (rich markup ok); `confirm` is a callable(prompt)->bool.
    Returns 0 when removal ran, 1 when the user aborted."""
    cfg = config.CONFIG_DIR
    cfg_exists = cfg.exists()

    emit("[bold]2b --rm[/bold] will remove:")
    emit(f"  • config directory {cfg}"
         f"{'' if cfg_exists else ' (not present)'} — API keys, prefs, MCP config")
    emit("  • the installed 2b executable (via 'uv tool uninstall 2b')")
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

    # 2) the executable — last, since uv removes files backing this running process
    #    (safe: the interpreter is already loaded; it just can't be re-run afterward).
    if shutil.which("uv"):
        try:
            r = subprocess.run(["uv", "tool", "uninstall", "2b"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                emit("[green]✓[/green] uninstalled the 2b executable")
            else:
                emit(f"[yellow]![/yellow] 'uv tool uninstall 2b' said: "
                     f"{(r.stderr or r.stdout).strip()[:200]}")
                emit("    if 2b was installed another way, remove it with that tool.")
        except Exception as e:
            emit(f"[yellow]![/yellow] could not run uv tool uninstall ({e}) — remove 2b manually.")
    else:
        emit("[yellow]![/yellow] uv not found — remove the executable yourself "
             "(e.g. [bold]uv tool uninstall 2b[/bold]).")

    emit("[green]Done. 2B has been removed.[/green]")
    return 0
