"""Pure unified-diff parsing for the TUI's inline, line-numbered review (Phase 4.2).

Dependency-free (no rich/textual) so it's unit-testable on its own; app_tui builds
the styled, line-numbered Text from these rows.
"""
import re

_HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def is_unified_diff(diff: str) -> bool:
    """True if this looks like a unified diff (has a hunk header) vs. a plain preview
    (e.g. a whole-file overwrite note), which is rendered without line numbers."""
    return "@@" in (diff or "")


def diff_counts(diff: str) -> tuple[int, int]:
    """(added, removed) content-line counts — file headers (+++/---) don't count."""
    add = rem = 0
    for line in (diff or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            add += 1
        elif line.startswith("-") and not line.startswith("---"):
            rem += 1
    return add, rem


def diff_rows(diff: str) -> list[tuple]:
    """Parse a unified diff into rows for line-numbered rendering. Each row is
    (old_no, new_no, kind, text) with kind in 'add'|'del'|'ctx'; line numbers are
    tracked from the @@ hunk headers (None on the side a row doesn't touch). Hunk
    headers and file headers are dropped — the caller shows its own summary."""
    rows: list[tuple] = []
    old = new = 0
    for line in (diff or "").splitlines():
        if line.startswith("@@"):
            m = _HUNK.match(line)
            if m:
                old, new = int(m.group(1)), int(m.group(2))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            rows.append((None, new, "add", line[1:]))
            new += 1
        elif line.startswith("-"):
            rows.append((old, None, "del", line[1:]))
            old += 1
        else:
            body = line[1:] if line.startswith(" ") else line
            rows.append((old, new, "ctx", body))
            old += 1
            new += 1
    return rows
