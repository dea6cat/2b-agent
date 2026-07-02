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
import shlex
import subprocess

MAX_FILE_BYTES = 200_000
GIT_TIMEOUT = 120
CMD_TIMEOUT = 600        # general shell commands (tests/builds can be slow)
# Always-safe inspection subcommands: run without confirmation and allowed in
# plan mode. Anything not here is treated as mutating (confirmed / plan-blocked).
READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "ls-files",
                 "rev-parse", "shortlog", "rev-list", "diff-tree", "describe"}
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
    {
        "type": "function",
        "function": {
            "name": "run_git",
            "description": (
                "Run a git command in the project (git only — no other shell commands). Pass the "
                "arguments that follow 'git', e.g. 'status', 'diff HEAD', 'add -A', "
                "'commit -m \"message\"', 'log --oneline -5'. Use this for all version-control actions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Arguments after 'git', e.g. 'status' or 'commit -m \"fix\"'"},
                },
                "required": ["args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command in the project — tests, build, git, formatters, anything. "
                "Returns combined stdout/stderr and the exit code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command, e.g. 'flutter test' or 'npm run build'"},
                },
                "required": ["command"],
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
    from . import symbols                                  # symbol outline: file shape + line anchors
    outline = symbols.outline(full)
    if outline and max_chars and len(note) + len(content) + len(outline) > max_chars:
        outline = ""                                       # never push the read past its budget
    return note + content + outline


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
    from . import repomap, symbols
    # Symbol enrichment only for bare-identifier queries — a definition of `query` is
    # tagged and floated to the top, so search_files doubles as go-to-definition without
    # a new tool. Phrase/regex-ish queries behave exactly as a plain literal search.
    tag_defs = symbols.is_identifier(query)
    matches = []                                  # (is_def, formatted_line)
    overflow = saw_def = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for fname in filenames:
            if _should_skip_file(fname):
                continue
            full = os.path.join(dirpath, fname)
            if _is_probably_binary(full):
                continue
            ext = os.path.splitext(fname)[1].lower()
            try:
                with open(full, "r", errors="ignore") as f:
                    for lineno, line in enumerate(f, start=1):
                        if query in line:
                            rel = os.path.relpath(full)
                            is_def = tag_defs and repomap.declared_name(line, ext) == query
                            saw_def = saw_def or is_def
                            marker = "▸def " if is_def else ""
                            matches.append((is_def, f"{marker}{rel}:{lineno}: {line.strip()[:300]}"))
                            if len(matches) > MAX_SEARCH_MATCHES:
                                overflow = True
                                break
            except (UnicodeDecodeError, IsADirectoryError, PermissionError):
                continue
            if overflow:
                break
        if overflow:
            break
    # Definitions first (stable within each group), then usages.
    ordered = [t for d, t in matches if d] + [t for d, t in matches if not d] if tag_defs \
        else [t for _, t in matches]
    # If the match cap may have hidden the definition, resolve it directly and point at it.
    header = ""
    if overflow and tag_defs and not saw_def:
        locs = symbols.definitions(query, root)
        if locs:
            header = "defined in: " + "; ".join(f"{l.path}:{l.line}" for l in locs[:5]) + "\n"
    if overflow:
        return (
            header + "\n".join(ordered[:MAX_SEARCH_MATCHES])
            + f"\n(stopped after {MAX_SEARCH_MATCHES} matches — too broad. "
            "Narrow the query, e.g. include a type/keyword, or add a more specific path.)"
        )
    return header + "\n".join(ordered) if ordered else f"no matches for '{query}' under {path}"


def _leading_ws(s: str) -> str:
    """The leading-whitespace prefix of a line (no trailing newline expected)."""
    return s[: len(s) - len(s.lstrip())]


def _line_offsets(lines: list[str]) -> list[int]:
    """Character offset in the joined text at which each line begins."""
    offs, acc = [], 0
    for ln in lines:
        offs.append(acc)
        acc += len(ln)
    return offs


def _block_hits(norm_content: list[str], norm_old: list[str]) -> list[int]:
    """Indices where norm_old occurs as a contiguous block in norm_content."""
    n = len(norm_old)
    if n == 0:
        return []
    return [i for i in range(len(norm_content) - n + 1) if norm_content[i:i + n] == norm_old]


def _shift_indent(text: str, add: str, cut: str) -> str:
    """Re-indent every non-blank line of `text`: drop a leading `cut` prefix, then
    prepend `add`. Blank lines are left untouched so we never create whitespace-only
    lines. Preserves each line's own trailing newline."""
    out = []
    for ln in text.splitlines(keepends=True):
        body = ln.rstrip("\r\n")
        nl = ln[len(body):]
        if not body.strip():
            out.append(ln)
            continue
        if cut and body.startswith(cut):
            body = body[len(cut):]
        if add:
            body = add + body
        out.append(body + nl)
    return "".join(out)


def _make_reindenter(file_line: str, old_line: str):
    """Build a render function that re-indents new_text from old_text's indentation to
    the file's actual indentation, inferred from the first matched line. Falls back to
    verbatim when the two indent prefixes aren't compatible (can't shift cleanly)."""
    fw = _leading_ws(file_line.rstrip("\r\n"))
    ow = _leading_ws(old_line)
    if fw == ow:
        return lambda nt: nt
    if fw.startswith(ow):
        return lambda nt: _shift_indent(nt, fw[len(ow):], "")
    if ow.startswith(fw):
        return lambda nt: _shift_indent(nt, "", ow[len(fw):])
    return lambda nt: nt


def _resolve_edit(content: str, old_text: str):
    """Locate the region of `content` that `old_text` refers to, tolerant of the
    whitespace drift small models produce. Tiers, tried in order:
      1. exact substring (unchanged fast path),
      2. trailing-whitespace + line-ending normalized (whole-line blocks),
      3. leading-indent-agnostic, re-indenting new_text to the file.
    Returns (start, end, render, note) for a unique match — content[start:end] is the
    span to replace and render(new_text) is the replacement; ("ambiguous", n) when a
    tier matches more than once; or None when nothing matched at any tier. Never
    resolves to a location when a tier is ambiguous — matching stays exactly-once."""
    count = content.count(old_text)
    if count == 1:
        s = content.index(old_text)
        return (s, s + len(old_text), lambda nt: nt, "")
    if count > 1:
        return ("ambiguous", count)

    content_lines = content.splitlines(keepends=True)
    old_lines = old_text.splitlines()
    if not old_lines:
        return None
    offsets = _line_offsets(content_lines)
    owns_trailing_nl = old_text.endswith(("\n", "\r"))
    tiers = (
        (lambda ln: ln.rstrip(), False, " (whitespace-tolerant match)"),
        (lambda ln: ln.strip(), True, " (indent-tolerant match)"),
    )
    for normalize, reindent, note in tiers:
        norm_content = [normalize(ln) for ln in content_lines]
        norm_old = [normalize(ln) for ln in old_lines]
        hits = _block_hits(norm_content, norm_old)
        if len(hits) > 1:
            return ("ambiguous", len(hits))
        if len(hits) == 1:
            i, n = hits[0], len(old_lines)
            start = offsets[i]
            matched = "".join(content_lines[i:i + n])
            end = start + len(matched)
            if not owns_trailing_nl:                    # keep the line break old_text didn't include
                end -= len(matched) - len(matched.rstrip("\r\n"))
            render = _make_reindenter(content_lines[i], old_lines[0]) if reindent else (lambda nt: nt)
            return (start, end, render, note)
    return None


def plan_edit(content: str, old_text: str, new_text: str):
    """Resolve where old_text applies in `content` and build the edited text. Pure —
    no file I/O — so the confirmation path (orchestrator.apply_edit) and the writer
    (do_edit_file) share one matching decision. Returns ('ok', new_content, note) or
    ('error', message) with the frozen, model-facing error strings."""
    resolved = _resolve_edit(content, old_text)
    if resolved is None:
        return ("error", "error: old_text not found in file — it must match exactly, including whitespace")
    if resolved[0] == "ambiguous":
        return ("error", f"error: old_text matches {resolved[1]} times — make it more specific so it matches exactly once")
    start, end, render, note = resolved
    return ("ok", content[:start] + render(new_text) + content[end:], note)


def do_edit_file(path, old_text, new_text, auto_yes):
    full = _safe_path(path)
    if full is None:
        return "error: empty or invalid path"
    if not os.path.isfile(full):
        return f"error: no such file: {path}"
    with open(full, "r", errors="replace") as f:
        content = f.read()
    status, *rest = plan_edit(content, old_text, new_text)
    if status == "error":
        return rest[0]
    new_content, note = rest
    diff = "\n".join(difflib.unified_diff(content.splitlines(), new_content.splitlines(), lineterm="", n=1))
    print(f"\n--- proposed edit: {path} ---\n{diff}")
    if not auto_yes:
        if input("Apply this edit? [y/N] ").strip().lower() != "y":
            return "edit rejected by user"
    with open(full, "w") as f:
        f.write(new_content)
    return f"edited {path}{note}"


def git_is_read_only(args: str) -> bool:
    """True if these git args are a known inspection-only subcommand (safe to run
    without confirmation and in plan mode). Anything else is treated as mutating."""
    try:
        parts = shlex.split(args or "")
    except ValueError:
        return False
    return bool(parts) and parts[0] in READ_ONLY_GIT


def do_run_git(args, max_chars=None):
    """Run `git <args>` in the project — git only, never a shell (no chaining or
    injection). Confirmation/plan-mode gating happens in the orchestrator; this
    just executes. Output (stdout+stderr) is capped and non-zero exit is flagged."""
    try:
        parts = shlex.split(args or "")
    except ValueError as e:
        return f"error: could not parse git args: {e}"
    if not parts:
        return "error: no git command given"
    try:
        proc = subprocess.run(["git", *parts], cwd=os.getcwd(), capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        return "error: git is not installed"
    except subprocess.TimeoutExpired:
        return f"error: git {parts[0]} timed out after {GIT_TIMEOUT}s"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip() or f"(git {parts[0]}: no output)"
    if max_chars and len(out) > max_chars:
        head, tail = out[: max_chars * 2 // 3], out[-max_chars // 3:]
        out = f"{head}\n… [git output truncated] …\n{tail}"
    return f"error: git exited {proc.returncode}\n{out}" if proc.returncode else out


def do_run_command(command, max_chars=None):
    """Run an arbitrary shell command in the project (cloud models only — see the
    orchestrator's model-aware tool exposure). Confirmation/plan gating happens
    upstream; this just executes. Output is capped and non-zero exit is flagged."""
    if not (command or "").strip():
        return "error: no command given"
    try:
        proc = subprocess.run(command, shell=True, cwd=os.getcwd(),
                              capture_output=True, text=True, timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {CMD_TIMEOUT}s"
    except Exception as e:
        return f"error: {e}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no output)"
    if max_chars and len(out) > max_chars:
        head, tail = out[: max_chars * 2 // 3], out[-max_chars // 3:]
        out = f"{head}\n… [output truncated] …\n{tail}"
    return f"error: command exited {proc.returncode}\n{out}" if proc.returncode else out
