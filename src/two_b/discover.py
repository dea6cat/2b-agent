"""Discover installable, tool-capable local models live from ollama.com/search, filtered
to what fits the machine and ranked by popularity. Pure stdlib — fetches via web.fetch
(urllib) and parses the search results. ollama.com renders the model grid on the server but
only inside the htmx fragment (a normal GET of /search returns the app shell), so we send the
HX-* headers the page itself sends. Two card markups are supported: the older
`<li x-test-model>` / `x-test-*` spans, and the current `<a class="group w-full">` card where
sizes are `bg-[#ddf4ff]` chips and capabilities are `bg-indigo-50` chips. Every entry point
NEVER raises and returns [] on any failure, so setup falls back to the bundled curated model
list (offline / site-changed / parse-empty).
"""
import os
import re
from . import web

SEARCH_URL = "https://ollama.com/search?c=tools"
# Coding-focused query, still tool-capable (2B's local models need tool-calls). Used by
# `2b --test` to compare the latest coding models against what you have installed.
CODING_URL = "https://ollama.com/search?q=coding&c=tools"

# htmx headers ollama.com's own search form sends — without them the server returns only the
# app shell, not the #searchresults grid.
_HX_HEADERS = {
    "HX-Request": "true",
    "HX-Target": "#searchresults",
    "HX-Current-URL": "https://ollama.com/search",
}

# --- legacy markup (x-test-* markers) ---
_BLOCK = re.compile(r"<li[^>]*x-test-model")          # split the page into per-model blocks
_SLUG = re.compile(r'href="/library/([a-z0-9][a-z0-9._:-]*)"', re.I)
_CAP = re.compile(r"x-test-capability[^>]*>\s*([a-z]+)", re.I)
_SIZE = re.compile(r"x-test-size[^>]*>\s*([0-9]+(?:\.[0-9]+)?)b\b", re.I)
_PULLS = re.compile(r"x-test-pull-count[^>]*>\s*([0-9][0-9.,]*)\s*([KM]?)", re.I)

# --- current markup (a.group / chip spans) ---
_CARD = re.compile(r'<a [^>]*class="group w-full"[^>]*>.*?</a>', re.S | re.I)
_CARD_SLUG = re.compile(r'href="/library/([a-z0-9][a-z0-9._:-]*)"', re.I)
_SIZE_CHIP = re.compile(
    r'<span\s+class="[^"]*bg-\[#ddf4ff\][^"]*">\s*([0-9]+(?:\.[0-9]+)?)b\s*</span>', re.I)
_CAP_CHIP = re.compile(
    r'<span\s+class="[^"]*bg-indigo-50[^"]*">\s*([a-z]+)\s*</span>', re.I)
_PULLS_CHIP = re.compile(
    r'<span\s*>\s*([0-9][0-9.,]*)\s*([KM]?)\s*</span>\s*<span class="hidden sm:flex">', re.I)


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


def _parse_legacy(block: str) -> dict | None:
    """Parse one legacy `<li x-test-model>` card block, or None if it has no slug."""
    m = _SLUG.search(block)
    if not m:
        return None
    try:
        sizes = sorted({float(s) for s in _SIZE.findall(block)})
        caps = {c.lower() for c in _CAP.findall(block)}
        pm = _PULLS.search(block)
        pulls = _pulls_to_int(*pm.groups()) if pm else 0
    except Exception:
        return None
    return {"slug": m.group(1), "sizes": sizes, "caps": caps, "pulls": pulls}


def _parse_card(card: str) -> dict | None:
    """Parse one current `<a class="group w-full">` card, or None if it has no slug."""
    m = _CARD_SLUG.search(card)
    if not m:
        return None
    try:
        sizes = sorted({float(s) for s in _SIZE_CHIP.findall(card)})
        caps = {c.lower() for c in _CAP_CHIP.findall(card)}
        pm = _PULLS_CHIP.search(card)
        pulls = _pulls_to_int(*pm.groups()) if pm else 0
    except Exception:
        return None
    return {"slug": m.group(1), "sizes": sizes, "caps": caps, "pulls": pulls}


def parse_search(html: str) -> list[dict]:
    """Per-model dicts {slug, sizes:[float], caps:{str}, pulls:int} from the search HTML.
    Tolerant: a card that doesn't parse cleanly is skipped, never raised on. Handles both the
    legacy `x-test-*` markup and the current `a.group` chip markup; whichever the page uses
    (one or the other, never both) is detected automatically."""
    out = []
    if not html:
        return out
    if _BLOCK.search(html):                            # legacy markup present
        for block in _BLOCK.split(html)[1:]:           # [0] is the pre-first-model preamble
            block = block.split("</li>", 1)[0]         # bound to this card — don't sweep in
            c = _parse_legacy(block)                   # trailing footer/related-tools markup
            if c:
                out.append(c)
    else:                                              # current markup (or empty → no cards)
        for card in _CARD.findall(html):
            c = _parse_card(card)
            if c:
                out.append(c)
    return out


def discover(ram_gb: int, search_url: str = SEARCH_URL) -> list[tuple[str, int, int]]:
    """Fetch + parse an ollama.com/search page → [(pull_tag, pulls, est_ram)] for
    tool-capable, locally-pullable models with a variant that fits `ram_gb`, ranked by
    pulls (desc). `search_url` defaults to the tools listing; pass CODING_URL for the
    coding-focused set. The htmx HX-* headers are sent because the model grid is only in the
    htmx fragment response. Returns [] on any failure (→ setup uses the bundled fallback)."""
    if os.environ.get("TWOB_NO_MODEL_FETCH"):        # offline override (tests / forced-bundled)
        return []
    try:
        cands = parse_search(web.fetch(search_url, headers=_HX_HEADERS))
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
