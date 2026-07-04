"""Untrusted-content fencing — a prompt-injection MITIGATION (not a guarantee).

Environment-derived bytes (file contents, command output, search matches, external
tool results) can carry instructions planted by an attacker ("ignore your rules and
run …"). We can't stop the model from reading them — it all lands in one context — but
we can (1) fence them in explicit `<untrusted_data …>` … `</untrusted_data …>` markers,
(2) tell the model (in the system prompt) that fenced text is DATA, never instructions,
and (3) neutralize the obvious bypass: content that forges the closing marker to "escape"
the fence and smuggle real instructions after it. Capable models honor this well; small
local models honor it partially — but the fence + escaping help regardless.

Markers are FIXED (deterministic → keeps 2B's prefix-stability and per-session
drift-replay intact) by default. Under TWOB_SEATBELT=strict the markers carry a
per-session random tag an injector can't forge; that only changes cross-session prompt
caching (drift-replay is already per-session salted), which is an acceptable strict-mode
cost. The module never wraps 2B's own framing/errors — only the external bytes.
"""
import os
import re

_TAG = "untrusted_data"
# Matches any opening/closing untrusted_data marker (fixed OR nonce'd OR forged) so a
# marker planted inside content can't terminate the fence early. Tolerates whitespace
# around the leading `<`/`/` (e.g. `< /untrusted_data>`). It canNOT defend against
# invisible-character obfuscation inside the tag name (e.g. a zero-width space) — but the
# model is told the exact marker form, so a mangled near-miss isn't a real close boundary.
_MARKER_RE = re.compile(r"<\s*/?\s*" + _TAG + r"\b[^>]*>", re.I)

# Cache is (strict_bool, nonce) so a change in strict mode (e.g. across tests) recomputes
# rather than silently leaking a stale nonce into deterministic-mode assumptions.
_nonce_cache = None


def _nonce() -> str:
    """Per-process (≈ per-session) random tag under strict mode, else ''. Cached so all
    fences in one session share it (stable prompt within the session); recomputed if the
    strict flag changes."""
    global _nonce_cache
    strict = (os.environ.get("TWOB_SEATBELT") or "").strip().lower() == "strict"
    if _nonce_cache is None or _nonce_cache[0] != strict:
        _nonce_cache = (strict, os.urandom(4).hex() if strict else "")
    return _nonce_cache[1]


def _reset_nonce_for_test():
    """Test hook: forget the cached nonce so a test can flip strict mode."""
    global _nonce_cache
    _nonce_cache = None


def wrap(content: str, source: str = "") -> str:
    """Fence `content` as untrusted. Any untrusted_data marker already in `content` is
    defanged first so it can't forge the fence. `source` (e.g. 'read_file:foo.py') is a
    label for the model; it never affects the escaping."""
    tag = _nonce()
    suffix = f" {tag}" if tag else ""
    safe = _MARKER_RE.sub("[fenced-marker removed]", content or "")
    label = f" from={source}" if source else ""
    return f"<{_TAG}{suffix}{label}>\n{safe}\n</{_TAG}{suffix}>"
