"""Discover installable, tool-capable local models live from ollama.com/search, filtered
to what fits the machine and ranked by popularity. Pure stdlib — fetches via web.fetch
(urllib) and parses the server-rendered HTML with regex keyed on ollama's stable
`x-test-*` markers (their own test hooks), so it's resilient to CSS/class churn. Every
entry point NEVER raises and returns [] on any failure, so setup falls back to the bundled
curated model list (offline / site-changed / parse-empty).
"""
import os
import re
from . import web

SEARCH_URL = "https://ollama.com/search?c=tools"
# Coding-focused query, still tool-capable (2B's local models need tool-calls). Used by
# `2b --test` to compare the latest coding models against what you have installed.
CODING_URL = "https://ollama.com/search?q=coding&c=tools"

_BLOCK = re.compile(r"<li[^>]*x-test-model")          # split the page into per-model blocks
_SLUG = re.compile(r'href="/library/([a-z0-9][a-z0-9._-]*)"', re.I)
_CAP = re.compile(r"x-test-capability[^>]*>\s*([a-z]+)", re.I)
_SIZE = re.compile(r"x-test-size[^>]*>\s*([0-9]+(?:\.[0-9]+)?)b\b", re.I)
_PULLS = re.compile(r"x-test-pull-count[^>]*>\s*([0-9][0-9.,]*)\s*([KM]?)", re.I)


def _pulls_to_int(num: str, suffix: str) -> int:
    try:
        n = float(num.replace(",", ""))
    except ValueError:
        return 0
    return int(n * {"k": 1_000, "m": 1_000_000}.get(suffix.lower(), 1))


def est_ram(params_b: float) -> int:
    """Rough RAM (GB) a model needs — weights (~Q4) + KV/overhead. Matches the curated
    numbers (4b→6, 8b→10, 9b→11, 12b→14, 14b→16): ≈ params + 2, min 4."""
    return max(4, round(params_b) + 2)


def _fmt(b: float) -> str:
    return str(int(b)) if float(b).is_integer() else str(b)


def fit_variant(sizes: list[float], ram_gb: int) -> float | None:
    """The largest parameter size that fits `ram_gb`, or None if none fit."""
    fits = [b for b in sizes if est_ram(b) <= ram_gb]
    return max(fits) if fits else None


def parse_search(html: str) -> list[dict]:
    """Per-model dicts {slug, sizes:[float], caps:{str}, pulls:int} from the search HTML.
    Tolerant: a block that doesn't parse cleanly is skipped, never raised on."""
    out = []
    if not html:
        return out
    for block in _BLOCK.split(html)[1:]:              # [0] is the pre-first-model preamble
        block = block.split("</li>", 1)[0]            # bound to this card — don't sweep in
        m = _SLUG.search(block)                        # trailing footer/related-tools markup
        if not m:
            continue
        try:
            sizes = sorted({float(s) for s in _SIZE.findall(block)})
            caps = {c.lower() for c in _CAP.findall(block)}
            pm = _PULLS.search(block)
            pulls = _pulls_to_int(*pm.groups()) if pm else 0
        except Exception:
            continue
        out.append({"slug": m.group(1), "sizes": sizes, "caps": caps, "pulls": pulls})
    return out


def discover(ram_gb: int, search_url: str = SEARCH_URL) -> list[tuple[str, int, int]]:
    """Fetch + parse an ollama.com/search page → [(pull_tag, pulls, est_ram)] for
    tool-capable, locally-pullable models with a variant that fits `ram_gb`, ranked by
    pulls (desc). `search_url` defaults to the tools listing; pass CODING_URL for the
    coding-focused set. Returns [] on any failure (→ setup uses the bundled fallback)."""
    if os.environ.get("TWOB_NO_MODEL_FETCH"):        # offline override (tests / forced-bundled)
        return []
    try:
        cands = parse_search(web.fetch(search_url))
        rows = []
        for c in cands:
            if "tools" not in c["caps"] or not c["sizes"]:   # need tool-use + a local variant
                continue
            b = fit_variant(c["sizes"], ram_gb)
            if b is None:
                continue
            rows.append((f"{c['slug']}:{_fmt(b)}b", c["pulls"], est_ram(b)))
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows
    except Exception:
        return []
