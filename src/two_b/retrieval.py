"""Dynamic dependency-ranked context retrieval — a host-side enrichment that, at the start of
a fresh task, points the model at the files most relevant to its request. Builds a regex import
graph, seeds from the task text (definitions + lexical match), ranks by graph proximity + lexical
relevance + path centrality, and injects a budget-capped pointer mini-map. The model still drives
all reading via read_file/search_files — this only seeds the starting point.

Frozen five-tool schema untouched; host-side only; stdlib only; never raises; opt out with
TWOB_NO_RETRIEVAL=1.
"""
import os
import re
from dataclasses import dataclass, field

from . import repomap, symbols, tools

MAX_PROJECT_SCAN = 4000       # bound the walk, like lsp._MAX_PROJECT_SCAN
GRAPH_RADIUS = 2              # BFS hops from a seed that still count as "near"

# Import forms per extension. Each pattern's group(1) is the module/path spec to resolve.
# The Python "from" pattern additionally captures group(2), the imported names, so
# `from pkg import b` can resolve to `pkg/b.py` and not just the `pkg` package itself.
IMPORT_PATTERNS: dict[str, list[re.Pattern]] = {
    ".py": [re.compile(r"^\s*from\s+([.\w]+)\s+import\s+(.+)"), re.compile(r"^\s*import\s+([.\w]+)")],
    ".js": [re.compile(r"""import\s+.*?from\s+['"]([^'"]+)['"]"""),
            re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")],
    ".go": [re.compile(r'^\s*"([^"]+)"')],   # inside import ( … ) blocks; best-effort by basename
    ".dart": [re.compile(r"""import\s+['"]([^'"]+)['"]""")],
    ".java": [re.compile(r"^\s*import\s+([\w.]+)\s*;")],
    ".rb": [re.compile(r"""require(?:_relative)?\s+['"]([^'"]+)['"]""")],
}
for _e in (".ts", ".jsx", ".tsx", ".mjs"):
    IMPORT_PATTERNS[_e] = IMPORT_PATTERNS[".js"]
IMPORT_PATTERNS[".kt"] = IMPORT_PATTERNS[".java"]


@dataclass
class Graph:
    root: str
    imports: dict = field(default_factory=dict)      # rel -> set(rel it imports)
    imported_by: dict = field(default_factory=dict)  # rel -> set(rel that import it)
    files: set = field(default_factory=set)          # all rel source paths (basename-indexed below)
    _by_stem: dict = field(default_factory=dict)     # basename-without-ext -> set(rel)


_cache: dict[tuple[str, str], Graph] = {}   # (root, signature) -> Graph, in-memory per process


def _iter_source_files(root: str):
    """Walk root (bounded, skipping dep/cache dirs) yielding (rel, ext) for known source files."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not tools._should_skip_dir(d)]
        for name in filenames:
            if tools._should_skip_file(name):
                continue
            ext = os.path.splitext(name)[1]
            if ext not in IMPORT_PATTERNS and ext not in repomap._PATTERNS:
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            yield rel, ext
            n += 1
            if n >= MAX_PROJECT_SCAN:
                return


def _signature(root: str) -> str:
    """Cheap digest of the walked tree (rel + mtime) so the cache rebuilds on any change."""
    import hashlib
    h = hashlib.sha256()
    for rel, _ext in sorted(_iter_source_files(root)):
        try:
            mt = os.path.getmtime(os.path.join(root, rel))
        except OSError:
            mt = 0
        h.update(f"{rel}:{mt}".encode())
    return h.hexdigest()


def _resolve_import(spec: str, src_rel: str, root: str, by_stem: dict, files: set) -> set:
    """Resolve one import spec from src_rel to repo file(s). Relative forms are resolved against
    the filesystem; package/absolute forms fall back to matching the last component's basename."""
    hits: set = set()
    # Relative JS/TS/Dart/Ruby ('./x', '../y')
    if spec.startswith("."):
        base = os.path.normpath(os.path.join(os.path.dirname(src_rel), spec.lstrip("./") if spec[:2] == "./" else spec))
        for cand in (base, f"{base}.py", f"{base}.js", f"{base}.ts", f"{base}.dart",
                     os.path.join(base, "index.js"), os.path.join(base, "index.ts"),
                     os.path.join(base, "__init__.py")):
            if cand in files:
                hits.add(cand)
        # Python dotted-relative already handled below; JS bare './util' handled here.
    # Python dotted module (a.b.c) — try a/b/c.py and a/b/c/__init__.py
    if not hits and "." in spec and "/" not in spec and not spec.startswith("."):
        as_path = spec.replace(".", os.sep)
        for cand in (f"{as_path}.py", os.path.join(as_path, "__init__.py")):
            if cand in files:
                hits.add(cand)
    # Fallback: match by final basename (package/absolute imports across languages)
    if not hits:
        stem = re.split(r"[./]", spec.strip("'\""))[-1]
        hits |= {r for r in by_stem.get(stem, set()) if r != src_rel}
    return hits


def _from_import_specs(base: str, names_part: str) -> list:
    """Expand `from BASE import a, b as c, (d)` into ['BASE.a', 'BASE.b', 'BASE.d'] so each
    imported name is also tried as a submodule path (e.g. `from pkg import b` -> `pkg.b`)."""
    specs = []
    sep = "" if base.endswith(".") else "."
    for nm in names_part.split(","):
        nm = nm.strip().strip("()").split(" as ")[0].strip()
        if not nm or nm == "*":
            continue
        specs.append(f"{base}{sep}{nm}")
    return specs


def _build(root: str) -> Graph:
    g = Graph(root=root)
    listing = list(_iter_source_files(root))
    g.files = {rel for rel, _ in listing}
    for rel, _ext in listing:
        stem = os.path.splitext(os.path.basename(rel))[0]
        g._by_stem.setdefault(stem, set()).add(rel)
    for rel, ext in listing:
        pats = IMPORT_PATTERNS.get(ext)
        if not pats:
            continue
        try:
            full = os.path.join(root, rel)
            if os.path.getsize(full) > tools.MAX_FILE_BYTES:
                continue
            with open(full, errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for line in text.splitlines():
            for pat in pats:
                m = pat.search(line)
                if not m:
                    continue
                specs = [m.group(1)]
                if pat.groups >= 2:
                    specs.extend(_from_import_specs(m.group(1), m.group(2)))
                for spec in specs:
                    for tgt in _resolve_import(spec, rel, root, g._by_stem, g.files):
                        if tgt == rel:
                            continue
                        g.imports.setdefault(rel, set()).add(tgt)
                        g.imported_by.setdefault(tgt, set()).add(rel)
    return g


def build_graph(root: str) -> Graph:
    """The project's file-level import graph (rel paths). Cached in-memory per (root, signature);
    rebuilt when the tree changes. Never raises — returns an empty Graph on any failure."""
    try:
        sig = _signature(root)
        key = (os.path.abspath(root), sig)
        cached = _cache.get(key)
        if cached is not None:
            return cached
        g = _build(root)
        _cache[key] = g
        return g
    except Exception:
        return Graph(root=root)


def bfs_distances(graph: Graph, seeds: set, radius: int = GRAPH_RADIUS) -> dict:
    """Hop distance from the nearest seed, traversing BOTH import directions, out to `radius`."""
    dist = {s: 0 for s in seeds if s in graph.files or s in graph.imports or s in graph.imported_by}
    frontier = set(dist)
    for d in range(1, radius + 1):
        nxt = set()
        for p in frontier:
            nxt |= graph.imports.get(p, set())
            nxt |= graph.imported_by.get(p, set())
        nxt -= set(dist)
        if not nxt:
            break
        for p in nxt:
            dist[p] = d
        frontier = nxt
    return dist


MAX_SEED_FILES = 20

# Words we never treat as code identifiers when seeding from prose.
_STOP = frozenset("""the a an and or of to in on for fix add edit make update change
implement create remove delete refactor investigate this that with into from flow bug issue
error test file code function class method""".split())
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def task_identifiers(task: str) -> list[str]:
    """Likely CODE identifiers in the task text — tokens with internal capitals (CamelCase),
    an underscore (snake_case), or a leading capital. Lowercase prose words are intentionally
    excluded (they'd trigger a full-tree symbols.definitions walk each, for little signal — path
    lexical matching covers them). Order-stable, deduped."""
    out, seen = [], set()
    for tok in _WORD.findall(task or ""):
        if tok in seen or len(tok) < 3 or tok.lower() in _STOP:
            continue
        looks_code = any(c.isupper() for c in tok[1:]) or "_" in tok or tok[0].isupper()
        if looks_code:
            seen.add(tok)
            out.append(tok)
    return out


def _lexical_seeds(task: str, graph: Graph) -> set:
    """Files whose path stem appears (case-insensitively) as a whole token in the task text."""
    terms = {w.lower() for w in _WORD.findall(task or "") if w.lower() not in _STOP and len(w) >= 3}
    hits = set()
    for rel in graph.files:
        stem = os.path.splitext(os.path.basename(rel))[0].lower()
        parts = set(re.split(r"[_\-.]", stem)) | {stem}
        if parts & terms:
            hits.add(rel)
    return hits


def seeds_from_task(task: str, root: str, graph: Graph) -> tuple:
    """(seed rel-paths, identifiers used). Seeds = files defining the task's identifiers
    (symbols.definitions, tiered LSP→MCP→regex) ∪ files whose name matches a task term. Capped."""
    ids = task_identifiers(task)
    seeds: set = set()
    for ident in ids:
        if not symbols.is_identifier(ident):
            continue
        for loc in symbols.definitions(ident, root):
            rel = os.path.relpath(loc.path, root) if os.path.isabs(loc.path) else loc.path
            seeds.add(rel)
    seeds |= _lexical_seeds(task, graph)
    seeds = {s for s in seeds if s in graph.files}     # keep only real graph nodes
    if len(seeds) > MAX_SEED_FILES:
        seeds = set(sorted(seeds)[:MAX_SEED_FILES])
    return seeds, ids


def candidate_files(graph: Graph, seeds: set) -> dict:
    """rel -> graph distance for the seed neighborhood (seeds at distance 0)."""
    return bfs_distances(graph, seeds)
