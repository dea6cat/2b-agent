"""`2b --doctor` — read-only environment diagnostics.

Kept out of cli.py (which imports prompt_toolkit at load) so it stays importable and
unit-testable on its own. Never mutates config or PATH — it only reports and prints the
exact fix; actually putting 2b on PATH is the installer's job, gated by consent.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from . import __version__, config, orchestrator, registry


def _bin_dir() -> str:
    """Where uv installs tool executables (fallback: ~/.local/bin)."""
    try:
        out = subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return os.path.expanduser("~/.local/bin")


def run(emit) -> int:
    """Print a ✓/✗ diagnostics report via `emit` (a print callable; rich markup allowed).
    Returns 0 if every check passes, else 1. Read-only — changes nothing."""
    ok = True
    emit(f"[bold]2b {__version__}[/bold]")

    # --- PATH: does `2b` resolve, and will it in new terminals? ----------
    resolved = shutil.which("2b")
    bindir = _bin_dir()
    on_path = bindir in os.environ.get("PATH", "").split(os.pathsep)
    if resolved:
        emit(f"[green]✓[/green] on PATH: {resolved}")
    else:
        ok = False
        emit("[red]✗[/red] '2b' is not on your PATH")
    if not on_path:
        ok = False
        emit(f"  fix: run [bold]uv tool update-shell[/bold] (adds {bindir}), then open a new terminal")
        emit(f'       or add to your shell profile: export PATH="{bindir}:$PATH"')

    # --- Ollama reachability ---------------------------------------------
    reg = registry.build_registry()
    ol = reg.get("ollama")
    try:
        n = len(ol.list_models()) if ol is not None else 0
        emit(f"[green]✓[/green] Ollama reachable — {n} local model(s)")
    except Exception as e:
        ok = False
        emit(f"[red]✗[/red] Ollama not reachable ({e}). Start it with [bold]ollama serve[/bold].")

    # --- configured providers --------------------------------------------
    live = sorted(registry.usable(reg))
    emit(f"    providers configured: {', '.join(live) if live else 'none'}")

    # --- default model (mirrors startup resolution) ----------------------
    saved = config.get_prefs().get("default_model")
    chosen, src = None, ""
    if saved and registry.resolve(reg, saved) is not None:
        chosen, src = saved, "saved default"
    else:
        try:
            chosen, src = orchestrator.pick_default_model(), "autodetected"
        except SystemExit as e:
            ok = False
            emit(f"[red]✗[/red] no default model available: {e}")
    if chosen:
        r = registry.resolve(reg, chosen)
        label = "" if r is None else (" (local)" if registry.is_local(r[0]) else " (cloud)")
        emit(f"[green]✓[/green] default model: {chosen}{label}  [{src}]")

    emit("[green]All checks passed.[/green]" if ok else "[yellow]Some checks need attention (see above).[/yellow]")
    return 0 if ok else 1
