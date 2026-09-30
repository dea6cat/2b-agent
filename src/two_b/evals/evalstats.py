"""Statistical rigor for the eval harness (P9): seeded bootstrap confidence
intervals, a McNemar paired test, across-seed variance, and an
environment snapshot that refuses to publish numbers from a dirty tree.

Pure and stdlib-only — no LLM judge, no numpy/scipy. The point is that 2B can publish
CI-bounded, reproducible numbers: every function here is deterministic given its inputs
(the bootstrap takes an explicit seed), so a reported interval can be regenerated exactly.
"""
from __future__ import annotations

import random
import subprocess
from math import comb
from statistics import mean, pstdev, pvariance

# --- confidence intervals + paired significance ------------------------------

def bootstrap_ci(values, *, n_resamples: int = 2000, seed: int = 0,
                 alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean of `values`, using a SEEDED RNG so the interval
    is reproducible. Returns (lo, hi). Degenerate inputs collapse to a point interval."""
    vals = [float(v) for v in values]
    if not vals:
        return (0.0, 0.0)
    if len(vals) == 1:
        return (round(vals[0], 4), round(vals[0], 4))
    rng = random.Random(seed)
    k = len(vals)
    means = sorted(sum(vals[rng.randrange(k)] for _ in range(k)) / k for _ in range(n_resamples))
    lo = means[int((alpha / 2) * n_resamples)]
    hi = means[min(n_resamples - 1, int((1 - alpha / 2) * n_resamples))]
    return (round(lo, 4), round(hi, 4))


def mcnemar(pairs) -> dict:
    """McNemar's exact paired test over (a_pass, b_pass) booleans. Counts discordant pairs
    — b (a right, b wrong) and c (b right, a wrong) — and returns a two-sided exact binomial
    p-value under H0: discordances split 50/50. p=1.0 when there are no discordant pairs."""
    b = sum(1 for a, x in pairs if a and not x)
    c = sum(1 for a, x in pairs if x and not a)
    n = b + c
    if n == 0:
        return {"b": 0, "c": 0, "n": 0, "p_value": 1.0}
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(k + 1)) / (2 ** n)
    return {"b": b, "c": c, "n": n, "p_value": round(min(1.0, 2 * tail), 4)}


def seed_summary(values) -> dict:
    """Mean and across-seed variance/stdev for a metric measured at N≥1 seeds. Variance and
    stdev are 0.0 for a single seed (nothing to vary), so a report can still print a row."""
    vals = [float(v) for v in values]
    if not vals:
        return {"mean": None, "variance": 0.0, "stdev": 0.0, "n": 0}
    multi = len(vals) > 1
    return {
        "mean": round(mean(vals), 4),
        "variance": round(pvariance(vals), 6) if multi else 0.0,
        "stdev": round(pstdev(vals), 4) if multi else 0.0,
        "n": len(vals),
    }


# --- environment snapshot + publish guard ------------------------------------

def _git_state() -> tuple[str | None, int | None]:
    """(HEAD sha, count of dirty working-tree entries). (None, None) outside a repo / on error."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        st = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10)
        if sha.returncode != 0 or st.returncode != 0:
            return None, None
        dirty = len([ln for ln in st.stdout.splitlines() if ln.strip()])
        return sha.stdout.strip(), dirty
    except Exception:
        return None, None


def env_snapshot(sampling: dict | None = None) -> dict:
    """A reproducibility snapshot for a published run: the git SHA, the dirty-file count, and
    the sampling params actually used. Attach this to any results file so a number is tied to
    the exact code and settings that produced it."""
    sha, dirty = _git_state()
    return {"git_sha": sha, "dirty_files": dirty, "sampling": dict(sampling or {})}


def can_publish(snapshot: dict) -> tuple[bool, str]:
    """Refuse to publish numbers from a dirty (or unknown) tree — a headline figure must be
    reproducible from a committed SHA. Returns (ok, reason)."""
    dirty = snapshot.get("dirty_files")
    if dirty is None:
        return False, "git state unknown — refusing to publish (results not tied to a commit)"
    if dirty > 0:
        return False, f"working tree is dirty ({dirty} uncommitted file(s)) — refusing to publish"
    return True, f"clean tree at {(snapshot.get('git_sha') or '')[:12]}"
