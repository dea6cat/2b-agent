"""Dependency-free repo map: regex symbol extraction + budget-ranked outline.

No tree-sitter, no ctags — just per-language regexes for top-level declarations.
Good-enough structure for orientation, and *always* bounded to a character
budget so it can never blow a small local model's context window. Used by /init
to build 2B.md and by the /map command for on-demand viewing.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache

from .tools import MAX_FILE_BYTES, _is_probably_binary, _should_skip_dir, _should_skip_file

# Top-level-ish declaration patterns per file extension. Kept deliberately simple
# — we want a skeleton, not a parser.
_PATTERNS: dict[str, list[str]] = {
    ".dart": [r"^\s*(?:abstract\s+|sealed\s+)?(?:class|mixin|enum|extension)\s+\w+",
              r"^\s*(?:Future|Stream|void|bool|int|double|String|Widget|List|Map|[A-Z]\w*)[\w<>,\s\?\[\]]*\s+\w+\s*\("],
    ".py": [r"^\s*(?:async\s+)?def\s+\w+", r"^\s*class\s+\w+"],
    ".js": [r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+",
            r"^\s*(?:export\s+)?class\s+\w+",
            r"^\s*(?:export\s+)?const\s+\w+\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"],
    ".go": [r"^\s*func\s+\w+", r"^\s*type\s+\w+\s+(?:struct|interface)"],
    ".rs": [r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+\w+", r"^\s*(?:pub\s+)?(?:struct|enum|trait|impl)\s+\w+"],
    ".rb": [r"^\s*(?:class|module|def)\s+\w+"],
    ".java": [r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|enum)\s+\w+"],
    ".swift": [r"^\s*(?:public|private|internal)?\s*(?:final\s+)?(?:class|struct|enum|protocol|extension|func)\s+\w+"],
    ".c": [r"^\s*[\w\*]+\s+\w+\s*\([^;]*\)\s*\{"],
}
_PATTERNS[".ts"] = _PATTERNS[".jsx"] = _PATTERNS[".tsx"] = _PATTERNS[".mjs"] = _PATTERNS[".js"]
_PATTERNS[".kt"] = _PATTERNS[".java"]
_PATTERNS[".cpp"] = _PATTERNS[".cc"] = _PATTERNS[".h"] = _PATTERNS[".hpp"] = _PATTERNS[".c"]

_ENTRY_STEMS = {"main", "index", "app", "lib", "mod", "cli"}
_IMPORTANT_TOPDIRS = {"lib", "src", "app", "cmd", "internal", "pkg"}
_MAX_SYMBOLS_PER_FILE = 20
_MAX_SYMBOL_LEN = 110


@lru_cache(maxsize=None)
def _compiled(ext: str):
    return tuple(re.compile(p) for p in _PATTERNS.get(ext, []))


def is_declaration(line: str, ext: str) -> bool:
    """True if `line` looks like a top-level declaration for this file type — the
    same structural test the repo map uses."""
    pats = _compiled(ext)
    return bool(pats) and any(p.match(line) for p in pats)


_KW_NAME_RE = re.compile(
    r"\b(?:class|mixin|enum|extension|interface|trait|impl|struct|module|def|func|fn|"
    r"type|const|let|var|protocol)\s+(\w+)")
_NAME_BEFORE_PAREN_RE = re.compile(r"(\w+)\s*\(")


def declared_name(line: str, ext: str) -> str | None:
    """The identifier a declaration line *declares* (not merely mentions), so a line
    like `def f(x: Session)` resolves to `f`, not `Session`. Returns None when the
    line isn't a declaration or no name can be pulled out. Regex, so imperfect on
    exotic signatures — advisory only."""
    if not is_declaration(line, ext):
        return None
    m = _KW_NAME_RE.search(line)          # keyword-led: class/def/func/type Name …
    if m:
        return m.group(1)
    m = _NAME_BEFORE_PAREN_RE.search(line)  # return-type-led / C-style: Type Name( …
    return m.group(1) if m else None


def symbols_with_lines(path: str) -> list[tuple[int, str]]:
    """Top-level declarations as (line_no, trimmed_decl) for a source file, capped.
    [] for unsupported types, binaries, or oversized files. A trailing (-1, "…")
    sentinel marks truncation. This is the single scanner; extract_symbols is a view."""
    ext = os.path.splitext(path)[1].lower()
    pats = _compiled(ext)
    if not pats:
        return []
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES or _is_probably_binary(path):
            return []
        with open(path, "r", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        if len(line) > 400:                       # skip minified / generated lines
            continue
        if any(p.match(line) for p in pats):
            sym = " ".join(line.strip().rstrip("{").strip().split())
            if sym:
                out.append((i, sym[:_MAX_SYMBOL_LEN]))
        if len(out) >= _MAX_SYMBOLS_PER_FILE:
            out.append((-1, "…"))
            break
    return out


def extract_symbols(path: str) -> list[str]:
    """Top-level declaration lines for a source file (trimmed, capped). [] for
    unsupported types, binaries, or oversized files."""
    return [sym for _, sym in symbols_with_lines(path)]


def _score(rel: str, symbols: int, focus: str) -> float:
    parts = rel.split(os.sep)
    depth = len(parts)
    stem = os.path.splitext(parts[-1])[0].lower()
    score = 5.0 - depth                            # shallower = more central
    if stem in _ENTRY_STEMS:
        score += 4
    if parts and parts[0] in _IMPORTANT_TOPDIRS:
        score += 3
    score += min(symbols, 10) * 0.2
    if focus and focus.lower() in rel.lower():
        score += 8
    return score


def build_map(root: str, budget_chars: int = 4000, focus: str = "") -> str:
    """A ranked, budget-bounded outline of the project's source files. Never
    exceeds budget_chars (roughly ~budget_chars/4 tokens)."""
    root = os.path.abspath(root)
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for fn in filenames:
            if _should_skip_file(fn) or os.path.splitext(fn)[1].lower() not in _PATTERNS:
                continue
            full = os.path.join(dirpath, fn)
            syms = extract_symbols(full)
            if not syms:
                continue
            rel = os.path.relpath(full, root)
            files.append((_score(rel, len(syms), focus), rel, syms))
    if not files:
        return "(no source symbols found — try /map <subdir> or search_files)"
    files.sort(key=lambda t: (-t[0], t[1]))

    out, used, shown = [], 0, 0
    for _, rel, syms in files:
        block = rel + "\n" + "\n".join(f"  {s}" for s in syms) + "\n"
        if used + len(block) > budget_chars and shown > 0:
            break
        out.append(block)
        used += len(block)
        shown += 1
    omitted = len(files) - shown
    tail = f"\n… +{omitted} more files with symbols (narrow with /map <subdir>)\n" if omitted > 0 else ""
    return "".join(out) + tail


# --- project stack detection (for /init) ------------------------------------

_STACK_MARKERS = [
    ("pubspec.yaml", "Dart/Flutter"),
    ("package.json", "Node/JavaScript"),
    ("pyproject.toml", "Python"),
    ("setup.py", "Python"),
    ("requirements.txt", "Python"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("pom.xml", "Java/Maven"),
    ("build.gradle", "Gradle/JVM"),
    ("Gemfile", "Ruby"),
    ("composer.json", "PHP"),
]


def detect_stack(root: str) -> list[str]:
    found = [f"{desc} ({marker})" for marker, desc in _STACK_MARKERS
             if os.path.isfile(os.path.join(root, marker))]
    return found


def top_dirs(root: str) -> list[str]:
    try:
        return sorted(d for d in os.listdir(root)
                      if os.path.isdir(os.path.join(root, d)) and not _should_skip_dir(d))
    except OSError:
        return []
