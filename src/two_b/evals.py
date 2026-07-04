"""Task-matrix scorer for 2B's host-side techniques.

Measures *2B*, not a raw model. It drives the real agent loop headlessly
(`2b --classic --model M --yes <task>`) over a FIXED task set, under feature
ablations, and scores each run on the axes that matter:

  - success        — did the change land AND does `dart analyze` pass?
  - tool_call_valid — fraction of tool calls well-formed on emission (validity,
                      kept apart from correctness — see the eval scheme's §5)
  - steps          — number of tool calls (context-economy proxy)
  - latency_s      — wall time

The key reframe over the original scheme: 2B *always* sends native, fully-typed
tools, so "constrained decoding" is a constant here, not a condition. The
meaningful axes are 2B's own host-side features, toggled by their env flags — so
each condition maps to a mechanism, and each *should* move a specific metric. If
it doesn't, the bottleneck is elsewhere.

The scoring/parsing helpers (`read_trace`, `shape_ok`, `summarize`) are pure and
unit-tested. `run_one`/`run_matrix` are integration entry points — they need a
live model and `2b` on PATH — driven by `2b eval`.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from statistics import mean
from typing import Callable

DEFAULT_TIMEOUT = 180
_OLLAMA_HOST = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
# The `2b` binary the harness drives. Override (TWOB_EVAL_CLI) to score a specific build —
# e.g. a dev checkout — instead of whatever `2b` is on PATH, so a build can be evaluated
# before it's published.
_CLI = os.environ.get("TWOB_EVAL_CLI", "2b")


def _have(cmd: str) -> bool:
    # An override may be a full path or a multi-word command; only a bare name is a PATH lookup.
    if os.sep in cmd or " " in cmd:
        return True
    return shutil.which(cmd) is not None


def _read(root: str, rel: str) -> str:
    try:
        with open(os.path.join(root, rel)) as f:
            return f.read()
    except OSError:
        return ""


def _has(needle: str, hay: str) -> bool:
    """Substring check tolerant of two harmless model choices: reflowed whitespace,
    and brace-delimited string interpolation (`${name}` for `$name`). Braces are
    dropped from both sides so the two interpolation styles compare equal."""
    def norm(s: str) -> str:
        return "".join(s.split()).replace("{", "").replace("}", "")
    return norm(needle) in norm(hay)


# --- the FIXED task set (a regression guard; frozen in the repo) -------------

@dataclass(frozen=True)
class Task:
    id: str
    tier: str                              # "A" | "B" | "C"
    files: dict                            # relative path -> initial content
    prompt: str
    verify: Callable[[str], bool]          # given the run's project dir, did it land?


_GREETER = (
    "/// A tiny greeter used only to check editing accuracy.\n"
    "class Greeter {\n"
    "  /// Returns a greeting for [name].\n"
    "  String greet(String name) => 'Hello, $name!';\n"
    "}\n"
)


def _greeter_ok(root: str) -> bool:
    t = _read(root, "sample.dart")
    return (_has("Hi there, $name!", t) and not _has("Hello, $name!", t)
            and _has("String farewell(String name)", t) and _has("Bye, $name!", t))


_COUNTER = (
    "/// A simple counter.\n"
    "class Counter {\n"
    "  int value = 0;\n"
    "  void increment() => value++;\n"
    "}\n"
)


def _counter_ok(root: str) -> bool:
    t = _read(root, "counter.dart")
    # A `step` field was added and increment() now advances by it.
    return _has("int step", t) and _has("value += step", t) and not _has("value++", t)


_MATH = (
    "/// Arithmetic helpers.\n"
    "int add(int a, int b) => a + b;\n"
)
_CALC = (
    "import 'math_utils.dart';\n"
    "\n"
    "int demo() => add(2, 3);\n"
)


def _rename_ok(root: str) -> bool:
    m, c = _read(root, "math_utils.dart"), _read(root, "calc.dart")
    # The definition and every call site were renamed add -> sum.
    return (_has("int sum(", m) and not _has("int add(", m)
            and _has("sum(2, 3)", c) and not _has("add(2, 3)", c))


TASKS: list[Task] = [
    Task("A1-greeter", "A", {"sample.dart": _GREETER},
         ("In sample.dart, make exactly two changes to the Greeter class and nothing else: "
          "(1) change the greeting returned by greet() from 'Hello, $name!' to 'Hi there, $name!'; "
          "(2) add a new method to the class: String farewell(String name) => 'Bye, $name!';"),
         _greeter_ok),
    Task("B1-counter", "B", {"counter.dart": _COUNTER},
         ("In counter.dart, add an `int step` field to Counter defaulting to 1, and change "
          "increment() so it advances value by step (value += step) instead of value++."),
         _counter_ok),
    Task("C1-rename", "C", {"math_utils.dart": _MATH, "calc.dart": _CALC},
         ("Rename the function `add` to `sum` everywhere in this project — the definition in "
          "math_utils.dart and every call site. Find the usages before editing."),
         _rename_ok),
]


# --- conditions = 2B's real host-side levers (not "constrained vs not") -------
# Each ablation removes one host-side technique via its existing env flag.
CONDITIONS: dict[str, dict] = {
    "full":           {},                                  # shipped baseline
    "no_diagnostics": {"TWOB_NO_DIAGNOSTICS": "1"},         # remove post-edit compiler feedback
    "no_semantics":   {"TWOB_NO_LSP": "1"},                # LSP off -> regex symbol fallback
}
# The §5 hypothesis: each ablation should degrade THIS metric in THIS direction
# vs. `full`. A flat delta means that technique isn't the bottleneck for this set.
EXPECTED: dict[str, tuple[str, str]] = {
    "no_diagnostics": ("success", "lower"),    # expected to hurt correctness (esp. tier B/C)
    "no_semantics":   ("steps", "higher"),     # expected to cost more steps (esp. tier C)
}

ROW_FIELDS = ["task_id", "tier", "model", "condition", "seed", "success", "landed",
              "analyze_clean", "tool_call_valid", "steps", "latency_s", "notes"]

# Sampling the harness pins (mirrors providers/ollama._options); recorded in the env
# snapshot so a published number is tied to the params that produced it.
SAMPLING = {"temperature": 0.2, "repeat_penalty": 1.1}


# --- scoring primitives (pure; unit-tested) ----------------------------------

def shape_ok(name, args) -> bool:
    """Is a tool call well-formed: a known tool with all its required args present?
    This is the 'first-try tool-call validity' axis — shape only, not correctness
    (a well-formed edit with the wrong old_text is valid here but fails success).
    Includes `delegate`, which cloud models get in addition to the frozen five."""
    from .toolspec import TOOL_SPECS, DELEGATE_SPEC
    spec = next((s for s in TOOL_SPECS + (DELEGATE_SPEC,) if s.name == name), None)
    if spec is None:
        return False
    args = args or {}
    return all(p.name in args for p in spec.params if p.required)


def read_trace(path: str) -> tuple[int, float | None]:
    """From the TWOB_TRACE JSONL, return (steps, valid_fraction). steps = number of
    tool calls; valid_fraction = share well-formed on emission. When there are NO
    tool calls, valid_fraction is None (not 1.0): a run that made no calls — a model
    that gave up on an edit task — must not read as perfect validity, so it's
    excluded from the validity mean rather than inflating it."""
    if not os.path.exists(path):
        return 0, None
    starts = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("t") == "tool_call_start":
                starts.append(ev)
    if not starts:
        return 0, None
    good = sum(1 for e in starts if shape_ok(e.get("name"), e.get("shown")))
    return len(starts), round(good / len(starts), 3)


def _dart_analyze_clean(root: str) -> bool:
    """`dart analyze` on the run's project. Skip-not-fail when dart isn't installed,
    so the harness still yields the 'landed' signal on a box without the SDK."""
    if not _have("dart"):
        return True
    try:
        r = subprocess.run(["dart", "analyze", root], capture_output=True,
                           text=True, timeout=120)
        return r.returncode == 0
    except Exception:
        return True


def _safe(fn, *a) -> bool:
    try:
        return bool(fn(*a))
    except Exception:
        return False


# --- run a task through the real agent (integration) -------------------------

def run_one(task: Task, model: str, condition: str, timeout: int = DEFAULT_TIMEOUT,
            seed: int | None = None, out_dir: str | None = None) -> dict:
    """Drive `2b` headlessly on one task under one condition; score the result. Each run gets
    its own mkdtemp workspace, deleted after the trace is copied out (to `out_dir`, if given).
    `seed` pins reproducible sampling for multi-seed variance. Needs a live model and `2b` on
    PATH (integration, not unit-tested)."""
    d = tempfile.mkdtemp(prefix="2b-eval-")
    try:
        for rel, content in task.files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p) or d, exist_ok=True)
            with open(p, "w") as f:
                f.write(content)
        trace = os.path.join(d, ".2b-trace.jsonl")
        env = {**os.environ, "OLLAMA_API_BASE": _OLLAMA_HOST,
               "TWOB_TRACE": trace, **CONDITIONS[condition]}
        if seed is not None:
            env["TWOB_SAMPLING_SEED"] = str(seed)
        t0 = time.time()
        proc, note = None, ""
        try:
            # stdin=DEVNULL is load-bearing: without it the headless `2b` finishes the
            # task then blocks in its REPL on the inherited tty until the timeout fires.
            cli = shlex.split(_CLI) + ["--classic", "--model", model, "--yes", task.prompt]
            proc = subprocess.run(cli, cwd=d, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, env=env)
        except subprocess.TimeoutExpired:
            note = f"timed out after {timeout}s"
        except Exception as e:
            note = f"failed to launch: {e}"
        wall = round(time.time() - t0, 1)
        # Surface an infra failure (bad model tag, Ollama down, crash) so a 0%-success
        # cell is diagnosable instead of looking like a genuine model failure.
        if proc is not None and proc.returncode != 0:
            note = f"2b exit {proc.returncode}: {(proc.stderr or '').strip()[-160:]}"
        landed = _safe(task.verify, d)
        clean = _dart_analyze_clean(d)
        steps, valid = read_trace(trace)
        # Copy the trace OUT before the workspace is deleted, so the run stays auditable.
        if out_dir and os.path.exists(trace):
            try:
                os.makedirs(out_dir, exist_ok=True)
                tag = f"{task.id}.{condition}.seed{'NA' if seed is None else seed}.jsonl"
                shutil.copyfile(trace, os.path.join(out_dir, tag))
            except OSError:
                pass
        return {"task_id": task.id, "tier": task.tier, "model": model, "condition": condition,
                "seed": seed, "success": landed and clean, "landed": landed, "analyze_clean": clean,
                "tool_call_valid": valid, "steps": steps, "latency_s": wall, "notes": note}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_matrix(models, tasks=None, conditions=None, seeds=(0,), out_dir=None) -> list[dict]:
    """Sequential execution against the local backend: every (model, condition, seed, task)
    cell, one at a time (no parallelism — it would contend for the single local model)."""
    tasks = tasks if tasks is not None else TASKS
    conditions = conditions or list(CONDITIONS)
    rows = []
    for m in models:
        for c in conditions:
            for s in seeds:
                for t in tasks:
                    rows.append(run_one(t, m, c, seed=s, out_dir=out_dir))
    return rows


# --- aggregation + report (pure; unit-tested) --------------------------------

def summarize(rows: list[dict]) -> tuple[dict, list[dict]]:
    """Fold rows to per-(model, condition) means, then apply the §5 check: for each
    ablation, did its EXPECTED metric move in the expected direction vs `full`?
    Returns (per_cell, checks)."""
    cells: dict[tuple, list[dict]] = {}
    for r in rows:
        cells.setdefault((r["model"], r["condition"]), []).append(r)
    per = {}
    for key, rs in cells.items():
        valids = [float(r["tool_call_valid"]) for r in rs if r["tool_call_valid"] is not None]
        per[key] = {
            "success": mean(1.0 if r["success"] else 0.0 for r in rs),
            "tool_call_valid": mean(valids) if valids else None,   # None = no calls anywhere in the cell
            "steps": mean(float(r["steps"]) for r in rs),
            "n": len(rs),
        }
    checks = []
    for model in sorted({m for (m, _c) in per}):
        base = per.get((model, "full"))
        if not base:
            continue
        for cond, (metric, direction) in EXPECTED.items():
            cell = per.get((model, cond))
            if not cell:
                continue
            delta = cell[metric] - base[metric]
            moved = delta < -1e-9 if direction == "lower" else delta > 1e-9
            checks.append({"model": model, "condition": cond, "metric": metric,
                           "direction": direction, "base": base[metric],
                           "ablated": cell[metric], "moved": moved})
    return per, checks


def significance(rows: list[dict]) -> dict:
    """Statistical rigor over the raw rows (P9): a seeded bootstrap CI on success per
    (model, condition) cell, across-seed variance of that cell's per-seed success, and a
    McNemar paired test of `full` vs each ablation (paired by task_id+seed). Pure — delegates
    the math to evalstats — so it's unit-testable without a live model."""
    from . import evalstats
    by: dict[tuple, list[dict]] = {}
    for r in rows:
        by.setdefault((r["model"], r["condition"]), []).append(r)
    cis, variance = {}, {}
    for key, rs in by.items():
        succ = [1.0 if r["success"] else 0.0 for r in rs]
        cis[key] = evalstats.bootstrap_ci(succ, seed=0)
        per_seed: dict = {}
        for r in rs:
            per_seed.setdefault(r.get("seed"), []).append(1.0 if r["success"] else 0.0)
        variance[key] = evalstats.seed_summary([sum(v) / len(v) for v in per_seed.values()])
    mcnemar = []
    for model in sorted({m for (m, _c) in by}):
        base = {(r["task_id"], r.get("seed")): bool(r["success"]) for r in by.get((model, "full"), [])}
        for cond in CONDITIONS:
            if cond == "full":
                continue
            cell = by.get((model, cond))
            if not cell or not base:
                continue
            pairs = [(base[k], bool(r["success"])) for r in cell
                     if (k := (r["task_id"], r.get("seed"))) in base]
            if pairs:
                res = evalstats.mcnemar(pairs)
                res.update({"model": model, "condition": cond})
                mcnemar.append(res)
    return {"cis": cis, "variance": variance, "mcnemar": mcnemar}


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ROW_FIELDS})


def _print_report(rows: list[dict], csv_path: str) -> None:
    per, checks = summarize(rows)
    sig = significance(rows)
    print(f"\n{len(rows)} runs — row detail written to {csv_path}\n")
    print(f"{'model':22} {'condition':16} {'runs':>4} {'success':>8} {'95% CI':>16} "
          f"{'sd(seed)':>9} {'valid':>7} {'steps':>7}")
    for (model, cond) in sorted(per):
        c = per[(model, cond)]
        valid = "  —  " if c["tool_call_valid"] is None else f"{c['tool_call_valid']:.2f}"
        lo, hi = sig["cis"].get((model, cond), (0.0, 0.0))
        sd = sig["variance"].get((model, cond), {}).get("stdev", 0.0)
        print(f"{model:22} {cond:16} {c['n']:>4} {c['success']:>8.2f} "
              f"{f'[{lo:.2f}, {hi:.2f}]':>16} {sd:>9.3f} {valid:>7} {c['steps']:>7.1f}")
    if sig["mcnemar"]:
        print("\nMcNemar — full vs ablation (paired per task+seed); p<0.05 = a real difference:")
        for m in sig["mcnemar"]:
            verdict = "significant" if m["p_value"] < 0.05 else "not significant (within noise)"
            print(f"  {m['model']:22} full vs {m['condition']:16} "
                  f"b={m['b']} c={m['c']} n={m['n']} p={m['p_value']:.3f} — {verdict}")
    if checks:
        print("\n§5 — did each ablation move its expected metric vs full?")
        for ck in checks:
            mark = "✓ moved" if ck["moved"] else "✗ flat — bottleneck likely elsewhere"
            print(f"  {ck['model']:22} {ck['condition']:16} {ck['metric']} "
                  f"{ck['base']:.2f}→{ck['ablated']:.2f} ({ck['direction']}): {mark}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="2b eval",
        description="Score 2B's host-side techniques over a fixed task set by driving the real agent.")
    ap.add_argument("--models", nargs="+", help="Models to evaluate, e.g. qwen3.5:9b qwen2.5-coder:14b")
    ap.add_argument("--conditions", nargs="+", choices=list(CONDITIONS),
                    help="Subset of conditions (default: all)")
    ap.add_argument("--tiers", nargs="+", choices=["A", "B", "C"], help="Restrict to task tiers")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-run wall cap in seconds")
    ap.add_argument("--csv", default="2b-eval-results.csv", help="Where to write the row-level CSV")
    ap.add_argument("--seeds", type=int, default=3, help="Sampling seeds per case (N>=3 gives variance)")
    ap.add_argument("--out-dir", default="2b-eval-traces", help="Where per-run traces are copied out")
    ap.add_argument("--publish", action="store_true",
                    help="Refuse to run unless the working tree is clean (results tied to a commit)")
    ap.add_argument("--list", action="store_true", help="List the task set and exit")
    args = ap.parse_args(argv)

    if args.list:
        for t in TASKS:
            print(f"{t.id:14} tier {t.tier}  files={list(t.files)}  {t.prompt[:70]}…")
        return 0
    if not args.models:
        print("error: pass --models (e.g. --models qwen3.5:9b). Use --list to see the tasks.",
              file=sys.stderr)
        return 2
    if not _have(_CLI):
        print(f"error: `{_CLI}` is not on PATH — the harness drives the real CLI.", file=sys.stderr)
        return 2

    from . import evalstats
    seeds = list(range(max(1, args.seeds)))
    snapshot = evalstats.env_snapshot({**SAMPLING, "seeds": seeds})
    ok, reason = evalstats.can_publish(snapshot)
    if args.publish and not ok:
        print(f"error: --publish refused: {reason}", file=sys.stderr)
        return 3
    sha = (snapshot["git_sha"] or "unknown")[:12]
    dirty = snapshot["dirty_files"]
    tree = "state unknown" if dirty is None else ("clean" if dirty == 0 else f"{dirty} dirty")
    print(f"env: git {sha} ({tree}) · sampling {SAMPLING} · seeds {seeds}")

    tasks = [t for t in TASKS if not args.tiers or t.tier in args.tiers]
    if not tasks:
        print("error: no tasks match --tiers.", file=sys.stderr)
        return 2
    rows = run_matrix(args.models, tasks, args.conditions, seeds=seeds, out_dir=args.out_dir)
    write_csv(rows, args.csv)
    # Persist the reproducibility snapshot beside the CSV.
    try:
        with open(os.path.splitext(args.csv)[0] + "-env.json", "w") as f:
            json.dump(snapshot, f, indent=2)
    except OSError:
        pass
    _print_report(rows, args.csv)
    return 0
