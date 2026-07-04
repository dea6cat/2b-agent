"""Deterministic, zero-model verification of edits — a host-side enrichment, never a
model-facing tool.

Complements diagnostics.py (external linters catch syntax/type errors) with the class
of problem a linter happily accepts but a user wouldn't: placeholder / stub markers the
model left behind while treating the task as done — `raise NotImplementedError`, a bare
`...` body, a "your code here" comment. A small model that stubs instead of implementing
then sees it on the same turn, in the tool result it already receives.

Also discovers the project's real check commands (test/lint) from its manifests, so a
capable model can be nudged to verify its own work before finishing.

Dep-free, host-side, opt out with TWOB_NO_VERIFY=1. Never raises.
"""
import json
import os
import re

# Markers of unfinished work that a syntax checker accepts. Kept deliberately narrow to
# HIGH-CONFIDENCE stub signals — explicit not-implemented raises and "implement this" /
# "your code here" placeholders — so an ordinary comment or identifier doesn't trip.
# Two weaker signals were deliberately NOT included: an aspirational `TODO: implement X
# later` (often on complete code) and a bare `...` body (idiomatic in Python Protocols /
# abstract methods) both false-positive too readily to be worth the nag.
_PLACEHOLDER = [
    (re.compile(r"\braise\s+NotImplementedError"), "raises NotImplementedError"),
    (re.compile(r"\bthrow\s+(?:UnimplementedError|UnsupportedError)"), "throws UnimplementedError"),
    (re.compile(r"(?i)your\s+(?:code|implementation)\s+(?:goes\s+)?here"), "'your code here' placeholder"),
    (re.compile(r"(?i)\bimplement(?:ation)?\s+(?:me|this|here)\b"), "'implement this' placeholder"),
]


def scan_text(text: str) -> list[str]:
    """Distinct placeholder/stub markers present in `text` (typically the new_text of an
    edit or a written file's contents). [] when clean or empty."""
    if not text:
        return []
    found, seen = [], set()
    for rx, label in _PLACEHOLDER:
        if label not in seen and rx.search(text):
            seen.add(label)
            found.append(label)
    return found


def summarize_edit(new_text: str) -> str:
    """A short note to append to an edit/write result when the just-applied text still
    contains placeholder/stub markers, so the model can finish the work this turn. ''
    when clean or opted out. Never raises."""
    if os.environ.get("TWOB_NO_VERIFY"):
        return ""
    try:
        found = scan_text(new_text)
    except Exception:
        return ""
    if not found:
        return ""
    return ("\n⚠ this change still contains placeholder/stub markers (" + "; ".join(found[:4])
            + ") — if the task expects working code, replace the stub with a real implementation.")


def _load_toml(path: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:                 # py < 3.11 — skip pyproject discovery
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def discover_checks(root: str = ".") -> list[str]:
    """Best-effort list of the project's real check commands, read from its manifests
    (package.json scripts, pyproject tools, pubspec). Used only to *suggest* a
    verification step — nothing is run here. Deduped, order-stable. Never raises."""
    cmds: list[str] = []

    def add(c: str) -> None:
        if c not in cmds:
            cmds.append(c)

    try:
        pj = os.path.join(root, "package.json")
        if os.path.isfile(pj):
            with open(pj, errors="replace") as f:
                scripts = (json.load(f).get("scripts") or {})
            for key in ("test", "lint", "typecheck", "check"):
                if key in scripts:
                    add(f"npm run {key}")

        ppt = os.path.join(root, "pyproject.toml")
        is_py = os.path.isfile(ppt) or os.path.isfile(os.path.join(root, "setup.py"))
        if is_py and os.path.isdir(os.path.join(root, "tests")):
            add("pytest")
        if os.path.isfile(ppt) and "ruff" in _load_toml(ppt).get("tool", {}):
            add("ruff check .")

        if os.path.isfile(os.path.join(root, "pubspec.yaml")):
            add("dart analyze")
            add("dart test" if os.path.isdir(os.path.join(root, "test")) else "flutter test")
    except Exception:
        pass
    return cmds
