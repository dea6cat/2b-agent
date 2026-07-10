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
import glob
import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass

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


FAST, TESTS = "fast", "tests"


def classify(cmd: str) -> str:
    """Best-effort tier for a user-supplied TWOB_VERIFY_CMD command (detected checks carry
    their own tier from discover_checks). Tests keyword wins so `swift test` -> tests while
    `swift build` -> fast; anything unmatched -> fast (conservative — usually quick)."""
    low = cmd.lower()
    return TESTS if ("test" in low or "spec" in low) else FAST


def _has_eslint_config(root: str) -> bool:
    return bool(glob.glob(os.path.join(root, ".eslintrc*"))
                or glob.glob(os.path.join(root, "eslint.config.*")))


def discover_checks(root: str = ".") -> list[tuple[str, str]]:
    """The project's real check commands as (command, kind) pairs — kind in {'fast','tests'},
    assigned by the detector (it knows the tool's semantics). Read from manifests; deduped,
    order-stable; fast checks before tests within a stack. Never raises."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(cmd: str, kind: str) -> None:
        if cmd not in seen:
            seen.add(cmd)
            out.append((cmd, kind))

    try:
        # Node / JS / TS — prefer the project's own scripts; fill gaps with direct tools.
        scripts: dict = {}
        pj = os.path.join(root, "package.json")
        if os.path.isfile(pj):
            with open(pj, errors="replace") as f:
                scripts = (json.load(f).get("scripts") or {})
            for key in ("lint", "typecheck", "check"):
                if key in scripts:
                    add(f"npm run {key}", FAST)
            if "test" in scripts:
                add("npm run test", TESTS)
        if os.path.isfile(os.path.join(root, "tsconfig.json")) and not ({"typecheck", "check"} & set(scripts)):
            add("tsc --noEmit", FAST)
        if _has_eslint_config(root) and "lint" not in scripts:
            add("eslint .", FAST)

        # Python
        ppt = os.path.join(root, "pyproject.toml")
        is_py = os.path.isfile(ppt) or os.path.isfile(os.path.join(root, "setup.py"))
        if os.path.isfile(ppt) and "ruff" in _load_toml(ppt).get("tool", {}):
            add("ruff check .", FAST)
        if is_py and os.path.isdir(os.path.join(root, "tests")):
            add("pytest", TESTS)

        # Dart / Flutter
        if os.path.isfile(os.path.join(root, "pubspec.yaml")):
            add("dart analyze", FAST)
            add("dart test" if os.path.isdir(os.path.join(root, "test")) else "flutter test", TESTS)

        # Go
        if os.path.isfile(os.path.join(root, "go.mod")):
            add("go build ./...", FAST)
            add("go test ./...", TESTS)

        # Swift (SwiftPM only; Xcode xcodebuild out of scope)
        if os.path.isfile(os.path.join(root, "Package.swift")):
            add("swift build", FAST)
            add("swift test", TESTS)

        # Kotlin / Gradle — `check` is test-inclusive (dependsOn test): one command, kind=tests.
        if os.path.isfile(os.path.join(root, "build.gradle.kts")) or os.path.isfile(os.path.join(root, "build.gradle")):
            gradle = "./gradlew" if os.path.isfile(os.path.join(root, "gradlew")) else "gradle"
            add(f"{gradle} check", TESTS)
    except Exception:
        pass
    return out


def discover_or_override(root: str = ".") -> list[tuple[str, str]]:
    """TWOB_VERIFY_CMD (';;'- or newline-separated) tagged via classify(), if set; else
    discover_checks(root). The escape hatch for stacks 2B can't auto-detect."""
    raw = os.environ.get("TWOB_VERIFY_CMD")
    if raw:
        cmds = [c.strip() for c in re.split(r";;|\n", raw) if c.strip()]
        return [(c, classify(c)) for c in cmds]
    return discover_checks(root)


VERIFY_TIMEOUT = 300   # per-command cap (seconds); a hung suite can't wedge the session


@dataclass(frozen=True, slots=True)
class CheckResult:
    cmd: str
    status: str   # "pass" | "fail" | "skipped" | "cancelled"
    output: str


def _truncate(s: str, cap: int = 4000) -> str:
    """Keep head + tail (compiler errors lead, test summaries trail). Never returns a string
    longer than the input — near the cap boundary the elision marker would otherwise inflate
    it, defeating the point of bounding the output."""
    if len(s) <= cap:
        return s
    out = f"{s[: cap * 2 // 3]}\n… [check output truncated] …\n{s[-cap // 3:]}"
    return out if len(out) < len(s) else s


def run_checks(checks, *, cancel=None, per_cmd_timeout: int = VERIFY_TIMEOUT, on_start=None):
    """Run each (cmd, kind) check host-side, returning a CheckResult per command. Missing
    binaries are skipped (not failed); a set `cancel` (task.cancel_flag) aborts promptly with
    `cancelled`. Never raises."""
    from . import tools
    results = []
    for cmd, _kind in checks:
        if cancel is not None and cancel.is_set():
            results.append(CheckResult(cmd, "cancelled", ""))
            break
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = cmd.split()
        first = parts[0] if parts else ""
        # shell=True returns exit 127 for a missing binary (no FileNotFoundError) — precheck.
        if not first or shutil.which(first) is None:
            results.append(CheckResult(cmd, "skipped", ""))
            continue
        if on_start:
            on_start(cmd)
        try:
            rc, out, status = tools._run_cancellable(cmd, shell=True, timeout=per_cmd_timeout,
                                                     cancel=cancel, env=tools._child_env())
        except Exception as e:
            results.append(CheckResult(cmd, "fail", f"could not run: {e}"))
            continue
        if status == "cancelled":
            results.append(CheckResult(cmd, "cancelled", ""))
            break
        if status in ("timeout", "kill_failed"):
            results.append(CheckResult(cmd, "fail", _truncate(out) + f"\n[{status} after {per_cmd_timeout}s]"))
            continue
        results.append(CheckResult(cmd, "pass" if rc == 0 else "fail", _truncate(out)))
    return results
