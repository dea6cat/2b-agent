"""The 5-tool schema and filesystem tool implementations.

Ported verbatim from the validated prototype (local_agent.py). This module is
deliberately frozen: the exact tool names, descriptions, schemas, and behavior
are what made small local models reliable, so they are not to be redesigned.
Only the transport layer (which provider serializes this schema) changes in
later milestones.
"""
import difflib
import os

MAX_FILE_BYTES = 200_000
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


def do_list_files(path):
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
    return "\n".join(out[:500]) or "(empty directory)"


def do_read_file(path):
    full = _safe_path(path)
    if full is None:
        return "error: empty or invalid path"
    if not os.path.isfile(full):
        return f"error: no such file: {path}"
    if os.path.getsize(full) > MAX_FILE_BYTES:
        return f"error: file too large ({os.path.getsize(full)} bytes) — point at a smaller file or section"
    if _is_probably_binary(full):
        return f"error: {path} looks like a binary file, not reading it"
    with open(full, "r", errors="replace") as f:
        return f.read()


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
