"""Host-side symbol resolution folded into search_files / read_file results.

Never a model-facing tool — the five-tool schema is unchanged. This is a
backend-agnostic resolver: the best available backend answers, the rest skip. Today
the regex backend (repomap's declaration patterns) is the always-available offline
floor; an LSP backend (lsp.py, a later phase) slots in ahead of it when a language
server is installed. Callers (do_search_files/do_read_file) just get richer strings.

A backend returns None to mean "I can't help here, try the next one" and a list
(possibly empty) to mean "I answered". The regex backend always answers.
"""
from __future__ import annotations

import os
import re
from typing import NamedTuple

from . import repomap

_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")
_MAX_DEFS = 10          # definition sites to surface at most
_OUTLINE_BUDGET = 600   # char cap for a read_file symbol outline (small-context safe)


class Loc(NamedTuple):
    path: str           # project-relative
    line: int
    text: str           # the declaration line (trimmed)


def is_identifier(query: str) -> bool:
    """Symbol enrichment only applies to bare identifiers — phrase/regex-ish queries
    behave exactly as a plain literal search."""
    return bool(_IDENT_RE.match(query or ""))


def _safe(backend, *args):
    try:
        return backend(*args)
    except Exception:
        return None


# --- regex backend (structural floor) ---------------------------------------

def _regex_definitions(identifier: str, cwd: str):
    """Declaration lines whose declared name *is* `identifier` — structural, not
    semantic (no scope/import awareness), but dependency-free and always available."""
    from .tools import _should_skip_dir, _should_skip_file
    hits: list[Loc] = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if _should_skip_file(fn) or not repomap._compiled(ext):
                continue
            full = os.path.join(dirpath, fn)
            for line_no, decl in repomap.symbols_with_lines(full):
                if line_no > 0 and repomap.declared_name(decl, ext) == identifier:
                    hits.append(Loc(os.path.relpath(full, cwd), line_no, decl))
                    if len(hits) >= _MAX_DEFS:
                        return hits
    return hits


def _regex_file_symbols(path: str):
    return [(ln, decl) for ln, decl in repomap.symbols_with_lines(path) if ln > 0]


def _lsp_definitions(identifier: str, cwd: str):
    from . import lsp                      # imported lazily so subprocess/threads load only when used
    return lsp.definitions(identifier, cwd)


def _mcp_definitions(identifier: str, cwd: str):
    from . import mcp_client               # host consumes an enabled MCP resolver tool itself
    hits = mcp_client.manager.resolve_symbol(identifier)
    if not hits:
        return None
    return [Loc(os.path.relpath(p, cwd) if os.path.isabs(p) else p, line, identifier)
            for p, line in hits] or None


# Backends, best-first: a direct LSP server (cleanest), then a curated MCP resolver if the
# user enabled one, then regex as the always-available offline floor. A backend returns None
# to defer to the next tier. definitions() gets the semantic tiers (an explicit "resolve this
# symbol", where import/scope accuracy matters); the file outline stays regex-only so
# read_file never pays a server spawn.
_DEF_BACKENDS = [_lsp_definitions, _mcp_definitions, _regex_definitions]
_FILE_BACKENDS = [_regex_file_symbols]


def definitions(identifier: str, cwd: str = ".") -> list[Loc]:
    """Best-available definition sites for `identifier`. [] when it's not an
    identifier or nothing resolves."""
    if not is_identifier(identifier):
        return []
    for backend in _DEF_BACKENDS:
        res = _safe(backend, identifier, cwd)
        if res is not None:
            return res
    return []


def file_symbols(path: str) -> list[tuple[int, str]]:
    """Best-available (line, decl) outline for a file."""
    for backend in _FILE_BACKENDS:
        res = _safe(backend, path)
        if res is not None:
            return res
    return []


def outline(path: str) -> str:
    """A compact, budget-bounded one-line symbol outline for appending to a whole-file
    read, or "" when there's too little structure to be worth the tokens."""
    syms = file_symbols(path)
    if len(syms) < 3:
        return ""
    parts, used = [], 0
    for ln, text in syms:
        entry = f"{ln} {text}"
        if used + len(entry) + 2 > _OUTLINE_BUDGET:
            parts.append("…")
            break
        parts.append(entry)
        used += len(entry) + 2
    return "\n\n# symbols: " + "; ".join(parts)
