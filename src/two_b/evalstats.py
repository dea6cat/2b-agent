"""Statistical rigor for the eval harness (P9): a deterministic exact-match scorer, seeded
bootstrap confidence intervals, a McNemar paired test, across-seed variance, and an
environment snapshot that refuses to publish numbers from a dirty tree.

Pure and stdlib-only — no LLM judge, no numpy/scipy. The point is that 2B can publish
CI-bounded, reproducible numbers: every function here is deterministic given its inputs
(the bootstrap takes an explicit seed), so a reported interval can be regenerated exactly.
"""
from __future__ import annotations

import random
import re
import string
import subprocess
from math import comb
from statistics import mean, pstdev, pvariance

# --- exact-match scorer (ports the official scheme; no LLM judge) ------------

_STRIP_CHARS = "$%,"                 # currency, percent, thousands separators
_LIST_SPLIT = re.compile(r"[,;]")
# A thousands-grouping comma is preceded by a digit and followed by EXACTLY three digits
# (real grouping is always 3-wide). This distinguishes '1,000' (a number) from '10,20' and
# '1,2,3' (lists) — a width-agnostic rule would wrongly collapse those lists into one scalar.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_PUNCT = set(string.punctuation)


def _norm_scalar(s) -> str:
    """Punctuation- and space-insensitive normal form: lowercase, drop `$ % ,`, then drop
    all remaining punctuation and whitespace. So '1,000' == '1000' and 'Yes.' == 'yes'."""
    s = str(s).strip().lower()
    for ch in _STRIP_CHARS:
        s = s.replace(ch, "")
    return "".join(c for c in s if c not in _PUNCT and not c.isspace())


def _is_listlike(s: str) -> bool:
    return "," in s or ";" in s


def exact_match(expected, got) -> bool:
    """The official exact-match scorer. A list-like expected answer (contains `,` or `;`)
    is split on `,`/`;` and compared element-wise as a normalized multiset with a length
    check; a scalar is compared punctuation/space-insensitively. Digit-grouping commas
    ('1,000') are stripped first so a number isn't misread as a list. Deterministic, no judge."""
    exp, gt = _THOUSANDS.sub("", str(expected)), _THOUSANDS.sub("", str(got))
    if _is_listlike(exp):
        # Keep every element (don't drop normalized-empty ones): the length check is only
        # meaningful if 'a,,b' (3 slots) and 'a,b' (2 slots) compare as different lengths.
        e = [_norm_scalar(p) for p in _LIST_SPLIT.split(exp)]
        g = [_norm_scalar(p) for p in _LIST_SPLIT.split(gt)]
        return len(e) == len(g) and sorted(e) == sorted(g)   # length check + element compare
    return _norm_scalar(exp) == _norm_scalar(gt)


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
