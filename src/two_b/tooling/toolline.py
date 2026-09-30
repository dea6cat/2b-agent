"""Pure one-line tool-result summaries for the TUI's tool lines (Phase 4.3 / Option B).

Dependency-free so it's unit-testable; app_tui appends the returned suffix to the
tool line (edit_file's +N -M comes from the diff, handled app-side). Empty string
means "nothing crisp to add" — the action phrase already names the tool + target.
"""
import re

_EXIT = re.compile(r"exited (\d+)")
_WROTE = re.compile(r"wrote (\d+) bytes")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def result_summary(name: str, result: str, ok: bool) -> str:
    result = result or ""
    if not ok:
        m = _EXIT.search(result)
        return f"exit {m.group(1)}" if m else "failed"
    if name == "read_file":
        return _plural(len(result.splitlines()), "line")
    if name == "list_files":
        return _plural(len([ln for ln in result.splitlines() if ln.strip()]), "item")
    if name == "write_file":
        m = _WROTE.search(result)
        return f"{m.group(1)} bytes" if m else ""
    # search_files (query is already in the phrase), run_git/run_command (ok): nothing to add.
    return ""
