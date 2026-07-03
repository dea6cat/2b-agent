"""`2b setup` — first-run onboarding, ported from install.sh so any install method
(pip / uv / git) gets the same graded local-model setup. install.sh now just bootstraps
the tool and delegates here. Shell/OS operations (installing Ollama, pulling models,
`uv tool update-shell`) are shelled out; grading, selection, and scoring are Python.

Prompts read `sys.stdin`; under `--yes` or a non-tty they collapse to documented defaults.
`run(opts)` mirrors install.sh's ordered flow and returns a process exit code.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass

from . import config
from .doctor import _bin_dir
from .providers.ollama import _total_ram_bytes

OLLAMA_HOST = (os.environ.get("OLLAMA_API_BASE") or os.environ.get("OLLAMA_HOST")
               or "http://localhost:11434")
DEFAULT_MODEL = "qwen3.5:9b"

# Competing agentic tools the clean-install can remove: (label, detect, uninstall argv, config dirs)
_OTHER_TOOLS = [
    ("opencode", ["opencode"], ["brew", "uninstall", "opencode"],
     ["~/.config/opencode", "~/.cache/opencode", "~/.local/state/opencode", "~/.local/share/opencode"]),
    ("Continue", ["cn"], ["npm", "uninstall", "-g", "@continuedev/cli"], ["~/.continue"]),
    ("Goose", ["goose"], ["brew", "uninstall", "block-goose-cli"],
     ["~/.config/goose", "~/.local/share/goose", "~/.local/state/goose"]),
    ("Cline", ["cline"], ["npm", "uninstall", "-g", "cline"], ["~/.cline"]),
    ("OpenHands", ["openhands"], ["uv", "tool", "uninstall", "openhands"], ["~/.openhands"]),
]


@dataclass(frozen=True)
class Model:
    name: str
    size: str
    min_ram_gb: int
    opt_in: bool
    note: str


CATALOG = [
    Model("qwen3:4b", "~2.6GB", 6, False, "small & fast — good for low-RAM machines"),
    Model("qwen3:8b", "~5.2GB", 10, False, "solid all-rounder"),
    Model("qwen3.5:9b", "~5.6GB", 11, False, "recommended — best balance in testing"),
    Model("gemma4:12b-mlx", "~8GB", 14, True, "opt-in — some machines show a cold-reload slowdown"),
    Model("qwen2.5-coder:14b", "~9GB", 16, True, "coder-focused — can slow on very large files"),
]


# --- pure logic (unit-tested) -----------------------------------------------

def machine() -> tuple[int, bool]:
    """(RAM in GiB, is_apple_silicon). RAM via os.sysconf (macOS + Linux)."""
    ram = _total_ram_bytes() or 0
    return ram // (1024 ** 3), (platform.system() == "Darwin" and platform.machine() == "arm64")


def fit_tag(min_ram_gb: int, ram_gb: int) -> str:
    if ram_gb >= min_ram_gb:
        return "✓ fits well"
    if ram_gb >= min_ram_gb - 3:
        return "~ tight"
    return f"✗ needs {min_ram_gb}GB+"


def default_index(ram_gb: int) -> int:
    """Largest non-opt-in model whose min-RAM fits; falls back to the first (smallest)."""
    best, idx = -1, 0
    for i, m in enumerate(CATALOG):
        if not m.opt_in and ram_gb >= m.min_ram_gb and m.min_ram_gb >= best:
            best, idx = m.min_ram_gb, i
    return idx


def parse_selection(text: str, default_idx: int) -> list[str]:
    """Empty → the default; 'all' → every model; otherwise 1-based indices (space/comma
    separated), invalid tokens ignored."""
    text = (text or "").strip()
    if not text:
        return [CATALOG[default_idx].name]
    if text.lower() == "all":
        return [m.name for m in CATALOG]
    out = []
    for tok in text.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(CATALOG):
            out.append(CATALOG[int(tok) - 1].name)
    return out


def default_model(selected: list[str], existing: list[str]) -> str:
    """Prefer qwen3.5:9b if present, else first selected, else first existing, else the tag."""
    pool = list(selected) + list(existing)
    if DEFAULT_MODEL in pool:
        return DEFAULT_MODEL
    return (selected or existing or [DEFAULT_MODEL])[0]


def grade_table(perf: dict, correctness: dict) -> tuple[list[str], str | None]:
    """perf: model -> (toks, mem, gpu); correctness: model -> (ok, wall). Returns
    (rendered rows, suggested fastest-passing model or None). VERDICT KEEP iff ok."""
    rows = ["  %-20s %8s  %-12s %-8s %-7s %s" % ("MODEL", "TOK/S", "MEMORY", "100%GPU", "CODING", "VERDICT")]
    best, bestv = None, -1.0
    for m, (ok, wall) in correctness.items():
        toks, mem, gpu = perf.get(m, ("?", "?", "?"))
        rows.append("  %-20s %8s  %-12s %-8s %-7s %s" % (m, toks, mem, gpu, f"{wall}s", "KEEP" if ok else "REMOVE"))
        if ok:
            try:
                if float(toks) > bestv:
                    bestv, best = float(toks), m
            except (TypeError, ValueError):
                if best is None:
                    best = m
    return rows, best


# --- interactive helpers ----------------------------------------------------

def _interactive(opts: dict) -> bool:
    return sys.stdin.isatty() and not opts.get("yes")


def _ask(prompt: str, default: str, opts: dict) -> str:
    if not _interactive(opts):
        return default
    try:
        ans = input(prompt).strip()
    except EOFError:
        return default
    return ans or default


def _confirm(prompt: str, default_yes: bool, opts: dict) -> bool:
    if not _interactive(opts):
        return default_yes
    hint = "Y/n" if default_yes else "y/N"
    try:
        ans = input(f"{prompt} [{hint}] ").strip().lower()
    except EOFError:
        return default_yes
    return default_yes if not ans else ans.startswith("y")


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# --- Ollama + shell operations ----------------------------------------------

def _server_up() -> bool:
    try:
        urllib.request.urlopen(OLLAMA_HOST + "/api/tags", timeout=2)
        return True
    except Exception:
        return False


def ensure_ollama(emit) -> bool:
    if _have("ollama"):
        return True
    try:
        if platform.system() == "Darwin" and _have("brew"):
            emit("Installing Ollama via Homebrew…")
            subprocess.run(["brew", "install", "ollama"], check=True)
        else:
            emit("Installing Ollama…")
            subprocess.run("curl -fsSL https://ollama.com/install.sh | sh", shell=True, check=True)
    except Exception as e:
        emit(f"Ollama install failed ({e}). Install it from https://ollama.com and re-run 2b setup.")
        return False
    return _have("ollama")


def ensure_server(emit) -> bool:
    if _server_up():
        return True
    if not _have("ollama"):
        return False
    emit("Starting the Ollama server…")
    try:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        return False
    for _ in range(15):
        if _server_up():
            return True
        time.sleep(1)
    return _server_up()


def installed_models() -> list[str]:
    if not _have("ollama"):
        return []
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    return [line.split()[0] for line in out.splitlines()[1:] if line.split()]


def pull(models: list[str], emit) -> None:
    for i, m in enumerate(models, 1):
        emit(f"[{i}/{len(models)}] Pulling {m} …")
        try:
            subprocess.run(["ollama", "pull", m])   # inherit stdout for the native progress bar
        except Exception as e:
            emit(f"warning: pull of {m} failed — {e}")


# --- self-test --------------------------------------------------------------

def _toks(model: str) -> float:
    payload = json.dumps({"model": model, "stream": False, "messages":
                          [{"role": "user", "content": "Write a one-sentence description of a binary search tree."}]}).encode()
    req = urllib.request.Request(OLLAMA_HOST + "/api/chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        ec, ed = d.get("eval_count", 0), d.get("eval_duration", 0) / 1e9
        return round(ec / ed, 1) if ed > 0 else 0.0
    except Exception:
        return 0.0


def _ps_mem_gpu(model: str) -> tuple[str, str]:
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return "?", "no"
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == model:
            mem = " ".join(parts[2:4]) if len(parts) >= 4 else "?"
            return mem, ("yes" if "100% GPU" in line else "no")
    return "?", "no"


_FIXTURE = ("/// A tiny greeter used only to check editing accuracy.\n"
            "class Greeter {\n"
            "  /// Returns a greeting for [name].\n"
            "  String greet(String name) => 'Hello, $name!';\n"
            "}\n")
_TASK = ("In sample.dart, make exactly two changes to the Greeter class and nothing else: "
         "(1) change the greeting returned by greet() from 'Hello, $name!' to 'Hi there, $name!'; "
         "(2) add a new method to the class: String farewell(String name) => 'Bye, $name!';")


def verdict(content: str) -> bool:
    """True iff the fixture shows both required edits (used by correctness_test)."""
    return ("Hi there, $name!" in content and "Hello, $name!" not in content
            and "farewell" in content and "Bye, $name!" in content)


def correctness_test(model: str) -> tuple[bool, int] | None:
    """Drive the installed 2b headlessly on a two-change task; verify the fixture.
    Returns (ok, wall_seconds), or None if 2b isn't on PATH."""
    if not _have("2b"):
        return None
    d = tempfile.mkdtemp()
    path = os.path.join(d, "sample.dart")
    with open(path, "w") as f:
        f.write(_FIXTURE)
    start = time.time()
    try:
        subprocess.run(["2b", "--classic", "--model", model, "--yes", _TASK], cwd=d,
                       capture_output=True, text=True, timeout=150,
                       env={**os.environ, "OLLAMA_API_BASE": OLLAMA_HOST})
    except Exception:
        pass
    wall = int(time.time() - start)
    content = ""
    try:
        with open(path) as f:
            content = f.read()
    except OSError:
        pass
    shutil.rmtree(d, ignore_errors=True)
    return verdict(content), wall


# --- clean-install + PATH ---------------------------------------------------

def clean_install(emit) -> None:
    for label, detect, uninstall, dirs in _OTHER_TOOLS:
        if _have(detect[0]):
            emit(f"Removing {label}…")
            try:
                subprocess.run(uninstall, capture_output=True, text=True)
            except Exception:
                pass
        for d in dirs:
            shutil.rmtree(os.path.expanduser(d), ignore_errors=True)


def fix_path(opts: dict, emit) -> None:
    bindir = _bin_dir()
    if bindir in os.environ.get("PATH", "").split(os.pathsep):
        return
    want = opts.get("fix_path")
    do = want == "yes" or (want is None and _confirm(
        "Put 2B on your PATH now (runs 'uv tool update-shell')?", True, opts))
    if do and _have("uv"):
        try:
            subprocess.run(["uv", "tool", "update-shell"], capture_output=True, text=True, timeout=30)
            emit("Added uv's tool directory to your PATH — open a new terminal.")
            return
        except Exception:
            pass
    emit(f"'2b' may not resolve in new terminals. Add {bindir} to your PATH:")
    emit("  uv tool update-shell")
    emit(f'  or: echo \'export PATH="{bindir}:$PATH"\' >> ~/.zshrc')


# --- driver -----------------------------------------------------------------

def run(opts: dict | None = None) -> int:
    """Ordered onboarding: clean → grade → select/reuse → ensure Ollama → pull →
    self-test/grade → persist default → PATH. Returns an exit code."""
    opts = opts or {}
    emit = print

    # 1) optional clean-install of other agentic tools
    clean = opts.get("clean")
    if clean == "yes" or (clean is None and _confirm(
            "Remove other agentic tools that proved unreliable with local models?", False, opts)):
        clean_install(emit)

    if opts.get("no_models"):
        fix_path(opts, emit)
        return 0

    ram_gb, apple = machine()
    emit(f"{platform.system()} {platform.machine()} · {ram_gb}GB RAM"
         + (" · Apple Silicon (Metal GPU)" if apple else ""))

    existing = installed_models() if _have("ollama") else []
    # existing-models reuse path
    if existing and not opts.get("models") and not _confirm(
            f"Found {len(existing)} installed model(s). Pull additional models?", False, opts):
        selected: list[str] = []
    else:
        if opts.get("models"):
            selected = list(opts["models"])
        else:
            di = default_index(ram_gb)
            emit("Models — pick what fits (grade is based on your RAM):")
            for i, m in enumerate(CATALOG, 1):
                tag = fit_tag(m.min_ram_gb, ram_gb)
                note = "already installed" if m.name in existing else m.note
                star = "*" if i - 1 == di else " "
                emit("  %s%d) %-20s %-8s  %-14s %s" % (star, i, m.name, m.size, tag, note))
            selected = parse_selection(
                _ask(f"Select by number (space-separated), 'all', or Enter for #{di + 1}: ",
                     str(di + 1), opts), di)

    if selected:
        if not ensure_ollama(emit) or not ensure_server(emit):
            emit("Ollama isn't available — cannot pull models. Fix that and re-run 2b setup.")
            return 1
        pull(selected, emit)

    # self-test + correctness grade (unless --no-benchmark)
    if selected and not opts.get("no_benchmark"):
        bench = [m for m in selected if m in installed_models()]
        if bench:
            emit("Self-test — tok/s + a real two-change edit through 2B (up to ~2 min per model)…")
            perf, correctness = {}, {}
            for m in bench:
                perf[m] = (_toks(m), *_ps_mem_gpu(m))
                ct = correctness_test(m)
                if ct is not None:
                    correctness[m] = ct
            if correctness:
                rows, best = grade_table(perf, correctness)
                for r in rows:
                    emit(r)
                if best:
                    emit(f"\nsuggested default: {best}")

    chosen = default_model(selected, existing)
    try:
        r = _prov_resolve(chosen)
        if r is not None:
            config.set_pref("default_model", f"{r[0].name}:{r[1]}")
    except Exception:
        pass

    fix_path(opts, emit)
    emit("\n2B is ready. Start it with:  2b")
    return 0


def _prov_resolve(name: str):
    from . import registry
    return registry.resolve(registry.build_registry(), name)


def main(argv: list[str] | None = None) -> int:
    """Parse `2b setup` flags (mirrors install.sh's) into opts and run onboarding."""
    import argparse
    p = argparse.ArgumentParser(prog="2b setup",
                                description="First-time setup: Ollama, model download, self-test, PATH.")
    p.add_argument("-y", "--yes", action="store_true", help="Accept defaults, no prompts")
    p.add_argument("--clean", dest="clean", action="store_const", const="yes",
                   help="Remove other agentic tools")
    p.add_argument("--no-clean", dest="clean", action="store_const", const="no")
    p.add_argument("--models", help="Space-separated model tags to pull (skips the menu)")
    p.add_argument("--no-models", action="store_true", help="Skip local model setup")
    p.add_argument("--no-benchmark", action="store_true", help="Skip the tok/s + correctness self-test")
    p.add_argument("--fix-path", dest="fix_path", action="store_const", const="yes",
                   help="Add uv's tool dir to PATH via 'uv tool update-shell'")
    p.add_argument("--no-fix-path", dest="fix_path", action="store_const", const="no")
    a = p.parse_args(argv)
    return run({
        "yes": a.yes, "clean": a.clean,
        "models": a.models.split() if a.models else None,
        "no_models": a.no_models, "no_benchmark": a.no_benchmark, "fix_path": a.fix_path,
    })
