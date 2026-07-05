"""`2b --test [<model>|auto]` — grade the local models you already have installed.

Runs the same tok/s + real two-change coding test that `2b setup` uses (so results
match), then prints the KEEP/REMOVE grade table and suggests the fastest passing model.

Modes:
  2b --test           grade every installed Ollama model
  2b --test <model>   grade just that one model
  2b --test auto       grade all, then offer to remove the ones that FAILED the coding
                       test (never the model you have set as default). --yes removes
                       them without the confirm prompt.

Kept out of cli.py (which imports prompt_toolkit at load) so it stays importable and
testable on its own. All the grading machinery is reused from setup.py.
"""
from __future__ import annotations

from . import config, setup


def _default_tag(prefs: dict) -> str:
    """The bare model tag the user has set as default (pref is stored provider-prefixed,
    e.g. 'ollama:qwen3:8b'); '' if none set."""
    pref = prefs.get("default_model", "") or ""
    return pref.split(":", 1)[1] if ":" in pref else pref


def run(emit, target: str = "", auto: bool = False,
        confirm=None, assume_yes: bool = False) -> int:
    """Grade installed models and print the table. `emit` is a print callable (rich
    markup ok); `confirm` is a callable(prompt)->bool used only in auto mode. Returns 0
    on a completed grading, 1 on a setup problem (no models / no server / 2b not on PATH).
    """
    installed = setup.installed_models()
    if not installed:
        emit("[yellow]No local models installed.[/yellow] "
             "Run [bold]2b setup[/bold] to install one, or [bold]ollama pull <model>[/bold].")
        return 1

    if target and target != "auto":
        if target not in installed:
            emit(f"[red]{target} isn't installed.[/red] Installed: {', '.join(installed)}")
            return 1
        models = [target]
    else:
        models = installed

    if not setup.ensure_server(emit):
        emit("[red]Ollama server isn't reachable — start it with 'ollama serve' and retry.[/red]")
        return 1

    emit(f"Testing {len(models)} model(s) — tok/s + a real two-change edit through 2B "
         "(up to ~2 min each)…")
    perf, correctness = {}, {}
    for m in models:
        perf[m] = (setup._toks(m), *setup._ps_mem_gpu(m))
        ct = setup.correctness_test(m)
        if ct is not None:
            correctness[m] = ct

    if not correctness:
        emit("[red]Could not run the coding test — the '2b' command isn't on your PATH.[/red] "
             "Run [bold]2b --doctor[/bold] for the fix, then retry.")
        return 1

    rows, best = setup.grade_table(perf, correctness)
    for r in rows:
        emit(r)
    if best:
        emit(f"\nsuggested default: [bold]{best}[/bold]  (set it with [bold]/default {best}[/bold])")

    if auto:
        protected = _default_tag(config.get_prefs())
        failed = [m for m, (ok, _) in correctness.items() if not ok]
        kept_default = [m for m in failed if m == protected]
        losers = [m for m in failed if m != protected]
        for m in kept_default:
            emit(f"[yellow]note:[/yellow] {m} failed but is your current default — keeping it. "
                 "Set another with [bold]/default[/bold], then re-run to remove it.")
        if not losers:
            emit("Nothing to remove — no failing models (other than a protected default).")
            return 0
        gb = round(sum(setup._gb_est(m) for m in losers), 1)
        do = assume_yes or (confirm is not None and confirm(
            f"Remove {len(losers)} model(s) that failed the coding test "
            f"({', '.join(losers)})? Frees ~{gb}GB"))
        if do:
            setup.remove_models(losers, emit)
        else:
            emit("Kept all models — nothing removed.")

    return 0
