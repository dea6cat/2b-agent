"""Post-edit diagnostics — a host-side enrichment, never a model-facing tool.

After a successful edit_file/write_file, the orchestrator calls `summarize(path)` and
folds the result into the tool-result string the model already receives. So a small
model that just introduced a syntax or type error sees it immediately, on the same
turn, without learning a new tool. The model's five-tool world is unchanged.

Kept deliberately safe for a small local context and a live loop: it only runs when a
checker for the file type is actually installed, it caps the number of issues, it uses
a short timeout, and it swallows every failure — no checker, missing binary, timeout,
or parse error yields an empty string, so it can never break or stall the edit loop.

Opt out entirely with TWOB_NO_DIAGNOSTICS=1.
"""
import os
import re
import shutil
import subprocess
import sys

TIMEOUT = 6           # checkers are near-instant warm; this only leaves headroom for a cold
                      # dart analysis-server spin-up, while bounding any stall a user would feel
MAX_ISSUES = 5        # cap so a badly-broken file can't flood a small local context
_MAX_MSG = 120        # per-issue message cap


def _ruff_argv(path):
    return ["ruff", "check", "--output-format=concise", path]


def _pycompile_argv(path):
    return [sys.executable, "-m", "py_compile", path]


def _dart_argv(path):
    return ["dart", "analyze", path]


_RUFF_RE = re.compile(r"^.+?:(\d+):\d+:\s+(.*)$")


def _parse_ruff(out):
    issues = []
    for line in out.splitlines():
        m = _RUFF_RE.match(line.strip())
        if m:
            issues.append(f"L{m.group(1)}: {m.group(2).strip()[:_MAX_MSG]}")
    return issues


def _parse_pycompile(out):
    # py_compile prints a traceback ending in a SyntaxError; pull the line + message.
    err = re.search(r"^(\w*Error): (.+)$", out, re.M)
    if not err:
        return []
    line = re.search(r"line (\d+)", out)
    msg = f"{err.group(1)}: {err.group(2).strip()}"[:_MAX_MSG]
    return [f"L{line.group(1)}: {msg}" if line else msg]


_DART_SEP_RE = re.compile(r"\s+[-•]\s+")   # ` - ` (current) or ` • ` (older dart)
_LOC_RE = re.compile(r":(\d+):\d+")


def _parse_dart(out):
    # `dart analyze` lines: "  error - <file>:<ln>:<col> - <message> - <code>".
    # Separator and field order have varied across versions, so locate the field
    # holding a "file:line:col" and take the longest word-bearing field as the message.
    issues = []
    for line in out.splitlines():
        parts = [p.strip() for p in _DART_SEP_RE.split(line.strip())]
        if len(parts) < 3:
            continue
        loc_idx = next((i for i, p in enumerate(parts) if _LOC_RE.search(p)), None)
        if loc_idx is None:
            continue
        ln = _LOC_RE.search(parts[loc_idx]).group(1)
        worded = [p for i, p in enumerate(parts) if i != loc_idx and " " in p]
        msg = max(worded, key=len) if worded else parts[min(loc_idx + 1, len(parts) - 1)]
        issues.append(f"L{ln}: {msg[:_MAX_MSG]}")
    return issues


# ext -> ordered candidates; the first whose binary is installed wins. Deliberately
# limited to checkers that work on a single file without project scaffolding — per-file
# `tsc`/`go vet` need package context, so they're intentionally omitted rather than
# half-working.
_CHECKERS = {
    ".py": [("ruff", _ruff_argv, _parse_ruff),
            (sys.executable, _pycompile_argv, _parse_pycompile)],
    ".dart": [("dart", _dart_argv, _parse_dart)],
}


def _pick(ext):
    for binary, argv_fn, parser in _CHECKERS.get(ext, ()):
        if shutil.which(binary):
            return argv_fn, parser
    return None


def check(path):
    """Return a list of "L<n>: <msg>" issue strings for `path`, or [] when there is no
    installed checker for its type, the file is clean, or anything goes wrong."""
    ext = os.path.splitext(path)[1].lower()
    picked = _pick(ext)
    if picked is None:
        return []
    argv_fn, parser = picked
    try:
        proc = subprocess.run(argv_fn(os.path.abspath(path)), cwd=os.getcwd(),
                              capture_output=True, text=True, timeout=TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    return parser((proc.stdout or "") + "\n" + (proc.stderr or ""))


def summarize(path):
    """A bounded one-line diagnostics summary to append to an edit result, or "" when
    there's nothing to say. Never raises — diagnostics must not break the edit loop."""
    if os.environ.get("TWOB_NO_DIAGNOSTICS"):
        return ""
    try:
        issues = check(path)
    except Exception:
        return ""
    if not issues:
        return ""
    shown = issues[:MAX_ISSUES]
    more = len(issues) - len(shown)
    tail = f" (+{more} more)" if more > 0 else ""
    return f"\n⚠ {len(issues)} issue(s) after edit: {'; '.join(shown)}{tail}"
