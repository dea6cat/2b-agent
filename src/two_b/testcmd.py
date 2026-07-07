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

import re

from . import config, discover, setup


_SIZE_RE = re.compile(r":(\d+(?:\.\d+)?)b", re.IGNORECASE)


def _fmt_pulls(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _tag_family_size(tag: str) -> tuple[str, float | None]:
    """(family, param-size) from a tag: 'qwen2.5-coder:14b' -> ('qwen2.5-coder', 14.0). Size
    is None when the tag carries no parseable Nb (e.g. ':latest')."""
    fam = tag.split(":", 1)[0]
    m = _SIZE_RE.search(tag)
    return fam, (float(m.group(1)) if m else None)


def _family_sizes(installed: list[str]) -> dict:
    """family -> largest installed param size (None if the family is present but unparseable)."""
    out: dict = {}
    for t in installed:
        fam, size = _tag_family_size(t)
        if fam not in out:
            out[fam] = size
        elif size is not None and (out[fam] is None or size > out[fam]):
            out[fam] = size
    return out


def _coding_report(emit, installed: list[str]) -> list:
    """Compare installed models to the latest tool-capable coding models on ollama.com that
    fit this machine. Print the comparison and return the actionable candidates as
    [(tag, pulls, ram, upgrade_from)] — a family you don't have (upgrade_from None) or a
    larger variant of one you do (upgrade_from = your current size). Ranked by pulls; empty
    if ollama.com is unreachable or you already run the best-fitting variants. Best-effort —
    never raises."""
    ram_gb, _ = setup.machine()
    found = discover.discover(ram_gb, discover.CODING_URL)   # best-fitting variant per family, RAM-filtered
    if not found:
        emit("\n[dim]Couldn't reach ollama.com to compare the latest coding models.[/dim]")
        return []
    have = _family_sizes(installed)
    cands = []
    for tag, pulls, ram in found:
        fam, size = _tag_family_size(tag)
        if fam not in have:                                  # a family you don't have at all
            cands.append((tag, pulls, ram, None))
        elif have[fam] is not None and size is not None and size > have[fam]:
            cands.append((tag, pulls, ram, have[fam]))       # a bigger variant of yours that still fits
    if not cands:
        emit("\n[dim]You already have the best-fitting coding models for this machine.[/dim]")
        return []
    emit("\n[bold]Latest coding models on ollama.com[/bold] (tool-capable, fit your RAM):")
    for tag, pulls, ram, up in cands[:5]:
        tail = f"  [dim](upgrade from :{discover._fmt(up)}b)[/dim]" if up is not None else ""
        emit(f"  [cyan]{tag}[/cyan]  {_fmt_pulls(pulls)} pulls  ~{ram}GB{tail}")
    return cands


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

    # Compare installed models to the latest coding models on ollama.com (best-fitting family
    # variant). `coding` is the actionable candidate list; in auto mode we pull+test the top one.
    coding = _coding_report(emit, installed)

    if auto:
        protected = _default_tag(config.get_prefs())
        failed = [m for m, (ok, _) in correctness.items() if not ok]
        kept_default = [m for m in failed if m == protected]
        losers = [m for m in failed if m != protected]
        for m in kept_default:
            emit(f"[yellow]note:[/yellow] {m} failed but is your current default — keeping it. "
                 "Set another with [bold]/default[/bold], then re-run to remove it.")
        if losers:
            gb = round(sum(setup._gb_est(m) for m in losers), 1)
            do = assume_yes or (confirm is not None and confirm(
                f"Remove {len(losers)} model(s) that failed the coding test "
                f"({', '.join(losers)})? Frees ~{gb}GB"))
            if do:
                setup.remove_models(losers, emit)
            else:
                emit("Kept all models — nothing removed.")
        else:
            emit("Nothing to remove — no failing models (other than a protected default).")

        # Don't recommend on RAM alone: pull the top coding candidate and run the real coding
        # test — recommend it only if it passes, remove it if it doesn't (auto's cleanup ethos).
        if coding:
            tag, _pulls, ram, _up = coding[0]
            if assume_yes or (confirm is not None and confirm(
                    f"Pull and coding-test {tag} (~{ram}GB download) to compare it to what you have?")):
                setup.pull([tag], emit)
                ct = setup.correctness_test(tag)
                if ct is None:
                    emit("[red]Couldn't run the coding test — '2b' isn't on your PATH.[/red]")
                elif ct[0]:
                    emit(f"[green]✔ {tag} passed the coding test[/green] — "
                         f"set it as default with [bold]/default {tag}[/bold]")
                else:
                    emit(f"[yellow]✗ {tag} failed the coding test — removing it.[/yellow]")
                    setup.remove_models([tag], emit)

    return 0
