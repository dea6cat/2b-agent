"""The 5-tool schema and filesystem tool implementations.

Ported verbatim from the validated prototype (local_agent.py). This module is
deliberately frozen: the exact tool names, descriptions, schemas, and behavior
are what made small local models reliable, so they are not to be redesigned.
Only the transport layer (which provider serializes this schema) changes in
later milestones.
"""
import difflib
import os
import re

MAX_FILE_BYTES = 200_000
_RANGE_RE = re.compile(r"^(?P<base>.+):(?P<start>\d+)-(?P<end>\d+)$")   # "path:90-120"
_MATCH_SCAN_CAP = 40   # stop walking once we've seen this many basename hits
SKIP_DIRS = {".git", "build", ".dart_tool", "node_modules", ".idea", ".aider.tags.cache.v4"}
SKIP_DIR_PREFIXES = (".aider",)  # e.g. .aider.tags.cache.v4, any future .aider* cache dirs
BINARY_PROBE_BYTES = 8192
MAX_SEARCH_MATCHES = 30

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files under a directory, recursively, relative to the current working directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory to list, e.g. 'lib/agent'"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full text contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search file contents for a literal substring across the project, recursively. "
                "Use this to find where something is defined or used before reading files one by one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Literal text to search for, e.g. 'MemoryScopeLevel'"},
                    "path": {"type": "string", "description": "Directory to search under (default: '.')"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact snippet of text in a file with new text, without rewriting the whole "
                "file. Prefer this over write_file for existing files, especially large ones — it's "
                "faster and lower-risk. old_text must match exactly once in the file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string", "description": "The exact existing text to replace."},
                    "new_text": {"type": "string", "description": "The text to replace it with."},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Overwrite a file with new full contents. Only for new files, or existing files small "
                "enough to safely reproduce in full. Prefer edit_file for existing files, especially "
                "large ones — regenerating a whole large file is slow and risks a truncated/incorrect result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "The complete new file contents."},
                },
                "required": ["path", "content"],
            },
        },
    },
]


def _safe_path(path):
    """Resolve a path to an absolute path — absolute inputs as-is, relative ones
    against the working directory, with ~ expanded. 2B is a personal local tool,
    so you can point it at files outside the working directory (writes are still
    confirmed via the UI). Returns None only for an empty/unusable path."""
    if not path or not str(path).strip():
        return None
    return os.path.abspath(os.path.expanduser(str(path)))


def _should_skip_dir(name):
    return name in SKIP_DIRS or name.startswith(SKIP_DIR_PREFIXES)


def _should_skip_file(name):
    # Aider leaves .aider.chat.history.md / .aider.input.history / etc. in any
    # project it's run in — noise for search_files/list_files, not real content.
    return name.startswith(SKIP_DIR_PREFIXES)


def _is_probably_binary(path):
    try:
        with open(path, "rb") as f:
            chunk = f.read(BINARY_PROBE_BYTES)
    except OSError:
        return True
    return b"\x00" in chunk


def _find_by_basename(name, given):
    """Files under the launch dir (the project) whose basename == name, skipping
    junk dirs. Matches whose relpath ends with the given path come first (handles
    a partial path like 'view/chat.dart'). Returns up to 20 relpaths."""
    root = os.getcwd()
    suffix_hits, base_hits, seen = [], [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for f in filenames:
            if _should_skip_file(f) or f != name:
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            (suffix_hits if given and given != name and rel.endswith(given) else base_hits).append(rel)
            seen += 1
        if seen >= _MATCH_SCAN_CAP:
            break
    return (suffix_hits + base_hits)[:20]


def do_list_files(path, max_chars=None):
    root = _safe_path(path)
    if root is None:
        return "error: empty or invalid path"
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for f in filenames:
            if _should_skip_file(f):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, f)))
    out.sort()
    listing = "\n".join(out[:500])
    if not listing:
        return "(empty directory)"
    # A big recursive listing is navigation noise that can blow a small model's
    # context — trim on a line boundary and tell it to narrow the path.
    if max_chars and len(listing) > max_chars:
        clipped = listing[:max_chars].rsplit("\n", 1)[0]
        return (clipped + f"\n…(large listing: {len(out)} files — pass a narrower path like 'lib' "
                f"to focus; showing the first {clipped.count(chr(10)) + 1})")
    return listing


def do_read_file(path, max_chars=None):
    """Read a file. Supports an optional line range via a 'path:START-END' suffix.
    If the path isn't found, looks for the basename anywhere in the project. When a
    whole-file read is too big for max_chars, suggests a section read instead of
    clipping. max_chars=None means no size guard (whole file returned)."""
    raw = str(path or "")
    lo = hi = None
    base = raw
    m = _RANGE_RE.match(raw)
    if m:
        base, lo, hi = m.group("base"), int(m.group("start")), int(m.group("end"))

    full = _safe_path(base)
    if full is None:
        return "error: empty or invalid path"

    note = ""
    if not os.path.isfile(full):                          # project-wide fallback
        name = os.path.basename(base)
        matches = _find_by_basename(name, base) if name else []
        if len(matches) == 1:
            note = f"[note: '{base}' not found at that path — reading the only project match: {matches[0]}]\n"
            full = _safe_path(matches[0])
        elif len(matches) > 1:
            return (f"error: no file at '{base}'. Files named '{name}' in this project: "
                    f"{', '.join(matches)}. Read one of these.")
        else:
            return f"error: no such file: {base}"

    if _is_probably_binary(full):
        return f"error: {base} looks like a binary file, not reading it"
    rel = os.path.relpath(full)

    if lo is not None:                                    # section read — never size-guarded
        seg = []
        with open(full, "r", errors="replace") as f:
            for i, line in enumerate(f, start=1):
                if i > hi:
                    break
                if i >= lo:
                    seg.append(line.rstrip("\n"))
        return f"{note}# {rel} lines {lo}-{hi}\n" + "\n".join(seg)

    if os.path.getsize(full) > MAX_FILE_BYTES:
        return f'error: file too large ({os.path.getsize(full)} bytes) — read a section, e.g. read_file "{rel}:1-200"'
    with open(full, "r", errors="replace") as f:
        content = f.read()
    if max_chars and len(content) > max_chars:            # too big for this model — suggest a section
        n_lines = content.count("\n") + 1
        return (f"{note}{rel} is {n_lines:,} lines (~{len(content) // 1024} KB) — too large to read whole "
                f'with the current model\'s context. Read a section, e.g. read_file "{rel}:1-150", or '
                "switch to a bigger model with /model.")
    return note + content


def do_write_file(path, content, auto_yes):
    full = _safe_path(path)
    if full is None:
        return "error: empty or invalid path"
    if content and not content.endswith("\n"):
        content += "\n"
    existing_lines = 0
    if os.path.isfile(full):
        with open(full, "r", errors="replace") as f:
            existing_lines = len(f.readlines())
    print(f"\n--- proposed write: {path} ({existing_lines} -> {len(content.splitlines())} lines) ---")
    if not auto_yes:
        if input("Apply this write? [y/N] ").strip().lower() != "y":
            return "write rejected by user"
    with open(full, "w") as f:
        f.write(content)
    return f"wrote {len(content)} bytes to {path}"


def do_search_files(query, path):
    root = _safe_path(path)
    if root is None:
        return "error: empty or invalid path"
    matches = []
    overflow = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for fname in filenames:
            if _should_skip_file(fname):
                continue
            full = os.path.join(dirpath, fname)
            if _is_probably_binary(full):
                continue
            try:
                with open(full, "r", errors="ignore") as f:
                    for lineno, line in enumerate(f, start=1):
                        if query in line:
                            rel = os.path.relpath(full)
                            matches.append(f"{rel}:{lineno}: {line.strip()[:300]}")
                            if len(matches) > MAX_SEARCH_MATCHES:
                                overflow = True
                                break
            except (UnicodeDecodeError, IsADirectoryError, PermissionError):
                continue
            if overflow:
                break
        if overflow:
            break
    if overflow:
        shown = matches[:MAX_SEARCH_MATCHES]
        return (
            "\n".join(shown)
            + f"\n(stopped after {MAX_SEARCH_MATCHES} matches — too broad. "
            "Narrow the query, e.g. include a type/keyword, or add a more specific path.)"
        )
    return "\n".join(matches) or f"no matches for '{query}' under {path}"


def do_edit_file(path, old_text, new_text, auto_yes):
    full = _safe_path(path)
    if full is None:
        return "error: empty or invalid path"
    if not os.path.isfile(full):
        return f"error: no such file: {path}"
    with open(full, "r", errors="replace") as f:
        content = f.read()
    count = content.count(old_text)
    if count == 0:
        return "error: old_text not found in file — it must match exactly, including whitespace"
    if count > 1:
        return f"error: old_text matches {count} times — make it more specific so it matches exactly once"
    new_content = content.replace(old_text, new_text, 1)
    diff = "\n".join(difflib.unified_diff(content.splitlines(), new_content.splitlines(), lineterm="", n=1))
    print(f"\n--- proposed edit: {path} ---\n{diff}")
    if not auto_yes:
        if input("Apply this edit? [y/N] ").strip().lower() != "y":
            return "edit rejected by user"
    with open(full, "w") as f:
        f.write(new_content)
    return f"edited {path}"
