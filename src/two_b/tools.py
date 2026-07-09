"""The 5-tool schema and filesystem tool implementations.

Ported verbatim from the validated prototype (local_agent.py). This module is
deliberately frozen: the exact tool names, descriptions, schemas, and behavior
are what made small local models reliable, so they are not to be redesigned.
Only the transport layer (which provider serializes this schema) changes in
later milestones.
"""
import difflib
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time

from . import seatbelt, untrusted

MAX_FILE_BYTES = 200_000
GIT_TIMEOUT = 120
CMD_TIMEOUT = 600        # general shell commands (tests/builds can be slow)
# Always-safe inspection subcommands: run without confirmation and allowed in
# plan mode. Anything not here is treated as mutating (confirmed / plan-blocked).
READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "ls-files",
                 "rev-parse", "shortlog", "rev-list", "diff-tree", "describe"}
_RANGE_RE = re.compile(r"^(?P<base>.+):(?P<start>\d+)-(?P<end>\d+)$")   # "path:90-120"
_MATCH_SCAN_CAP = 40   # stop walking once we've seen this many basename hits
SKIP_DIRS = {".git", "build", ".dart_tool", "node_modules", ".idea", ".aider.tags.cache.v4",
             # Python virtualenvs and caches: dependency/generated trees, not project
             # source — walking them pollutes search/list and lets a basename fallback
             # resolve an imprecise read to a site-package file.
             ".venv", "venv", "site-packages", "__pycache__",
             ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".eggs"}
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


def resolve_read_path(path):
    """The absolute path do_read_file actually reads for `path`: the given location,
    or — when that doesn't exist — the unique project file matching its basename.
    None when empty, missing, or ambiguous. Mirrors do_read_file's resolution (keep
    the two in sync) so read-tracking keys on the exact file the model was shown."""
    m = _RANGE_RE.match(str(path or ""))
    base = m.group("base") if m else str(path or "")
    full = _safe_path(base)
    if full is None:
        return None
    if os.path.isfile(full):
        return full
    name = os.path.basename(base)
    matches = _find_by_basename(name, base) if name else []
    return _safe_path(matches[0]) if len(matches) == 1 else None


def do_read_file(path, max_chars=None):
    """Read a file. Supports an optional line range via a 'path:START-END' suffix.
    If the path isn't found, looks for the basename anywhere in the project (keep this
    fallback in sync with resolve_read_path). When a whole-file read is too big for
    max_chars, suggests a section read instead of clipping. max_chars=None means no
    size guard (whole file returned)."""
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
        return f"{note}# {rel} lines {lo}-{hi}\n" + untrusted.wrap("\n".join(seg), f"read_file:{rel}")

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
    # Fence content AND the outline together: the outline is derived from the (untrusted)
    # file bytes (raw declaration lines), so it must live INSIDE the fence — appending it
    # after the close would smuggle attacker text into a trusted-looking region.
    return note + untrusted.wrap(content + outline, f"read_file:{rel}")


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
    new_lines = len(content.splitlines())
    print(f"\n--- proposed write: {path} ({existing_lines} -> {new_lines} lines) ---")
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
            header + untrusted.wrap("\n".join(ordered[:MAX_SEARCH_MATCHES]), f"search_files:{query}")
            + f"\n(stopped after {MAX_SEARCH_MATCHES} matches — too broad. "
            "Narrow the query, e.g. include a type/keyword, or add a more specific path.)"
        )
    if not ordered:
        return f"no matches for '{query}' under {path}"
    return header + untrusted.wrap("\n".join(ordered), f"search_files:{query}")


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
    # A blank line at the end of old_text (a stray extra newline a small model
    # appended, e.g. "…}\n\n") has no counterpart in the file, so the block match
    # below would never land and the model bounces off "old_text not found" and
    # retries the same near-miss. Drop trailing blank lines so the tolerant tiers
    # match the real text; keep at least one line so a whitespace-only old_text is
    # left for the caller to reject.
    while len(old_lines) > 1 and not old_lines[-1].strip():
        old_lines.pop()
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


def _nearest_hint(content: str, old_text: str) -> str:
    """When old_text didn't match even the tolerant tiers, show the closest real
    region (the best-matching line ±3 lines, numbered) so the model can re-copy it
    exactly instead of re-guessing the same near-miss (small models otherwise loop
    on 'not found'). Empty when nothing is close enough."""
    old_lines = [ln for ln in old_text.splitlines() if ln.strip()]
    file_lines = content.splitlines()
    if not old_lines or not file_lines:
        return ""
    needle = old_lines[0].strip()
    stripped = [ln.strip() for ln in file_lines]
    match = difflib.get_close_matches(needle, stripped, n=1, cutoff=0.6)
    if not match:
        return ""
    i = stripped.index(match[0])
    lo, hi = max(0, i - 3), min(len(file_lines), i + 4)
    width = len(str(hi))
    snippet = "\n".join(f"{n + 1:>{width}} | {file_lines[n]}" for n in range(lo, hi))
    return (f"\nThe closest match is around line {i + 1}. Lines {lo + 1}-{hi}:\n{snippet}\n"
            "Read the file again and copy old_text exactly from there (including indentation).")


def plan_edit(content: str, old_text: str, new_text: str):
    """Resolve where old_text applies in `content` and build the edited text. Pure —
    no file I/O — so the confirmation path (orchestrator.apply_edit) and the writer
    (do_edit_file) share one matching decision. Returns ('ok', new_content, note) or
    ('error', message) with the frozen, model-facing error strings.

    Line endings are normalized to LF for matching so a small model's LF old_text
    lands cleanly on a CRLF file; a *uniformly* CRLF file has its CRLF restored on
    the result (no silent flip). Detection requires uniformity, not mere presence —
    a lone stray CRLF in an otherwise-LF file must not flip the whole file to CRLF."""
    crlf, lf = content.count("\r\n"), content.count("\n")
    eol = "\r\n" if crlf and crlf == lf else "\n"   # every \n is part of a \r\n
    norm = content.replace("\r\n", "\n")
    norm_old = old_text.replace("\r\n", "\n")
    norm_new = new_text.replace("\r\n", "\n")
    resolved = _resolve_edit(norm, norm_old)
    if resolved is None:
        return ("error", "error: old_text not found in file — it must match exactly, including whitespace"
                + _nearest_hint(norm, norm_old))
    if resolved[0] == "ambiguous":
        return ("error", f"error: old_text matches {resolved[1]} times — make it more specific so it matches exactly once")
    start, end, render, note = resolved
    rendered = render(norm_new)
    # Drift guard: old_text replaced whole line(s) (it ended in a newline) but new_text
    # dropped the trailing newline, and real content follows the match — so that next
    # line would merge onto new_text (e.g. "B\n"->"B2" turning "A\nB\nC" into "A\nB2C").
    # A small model almost always meant to replace the line, not join the next one, so
    # keep the boundary. Skipped when the next char is already a newline (no merge) or
    # nothing follows (would add a spurious trailing blank).
    if (norm_old.endswith("\n") and rendered and not rendered.endswith("\n")
            and end < len(norm) and norm[end] != "\n"):
        rendered += "\n"
    result = norm[:start] + rendered + norm[end:]
    if eol == "\r\n":
        result = result.replace("\n", "\r\n")
    return ("ok", result, note)


def do_edit_file(path, old_text, new_text, auto_yes):
    full = _safe_path(path)
    if full is None:
        return "error: empty or invalid path"
    if not os.path.isfile(full):
        return f"error: no such file: {path}"
    # newline="" keeps CRLF visible to plan_edit so it can detect the file's ending
    # and preserve it; universal-newline mode would strip \r before we ever see it.
    with open(full, "r", errors="replace", newline="") as f:
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
    with open(full, "w", newline="") as f:                 # write bytes verbatim — no line-ending translation
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


def _killpg(proc) -> bool:
    """Kill a process and its whole group: SIGTERM, a short grace, then SIGKILL.
    Grouping (see `start_new_session=True` below) means a shell's children —
    npm, pytest, a compiler — die with it instead of being orphaned. Returns True
    when the process is confirmed dead, False when it may still be running — e.g. a
    group we lack permission to signal (a `sudo`'d child), or one wedged in
    uninterruptible I/O that even SIGKILL can't preempt — so the caller can report
    honestly instead of claiming it stopped."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return True   # already gone
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True   # exited between checks
        except OSError:
            # e.g. PermissionError signalling a root-owned group — we can't kill it.
            return proc.poll() is not None
        try:
            proc.wait(timeout=0.5)
            return True
        except subprocess.TimeoutExpired:
            continue
    return proc.poll() is not None   # SIGKILL didn't take (rare: D-state I/O wait)


# --- subprocess environment hygiene (S2) ------------------------------------
# Ambient shell secrets (AWS_*, GH_TOKEN, exported *_API_KEY, …) must never reach a
# command 2B spawns. Default: drop credential-shaped var NAMES (usable — build tools
# that need JAVA_HOME/VIRTUAL_ENV/etc. keep working). strict (TWOB_SEATBELT=strict):
# keep only a known-safe allowlist. TWOB_NO_ENV_SCRUB=1 inherits the full environment.
_ENV_SECRET_RE = re.compile(r"(?i)(?:^|_)(?:KEY|SECRET|TOKEN|PASSWD|PASSWORD|CREDENTIALS?|APIKEY)(?:$|_)")
_ENV_ALLOW = {
    "PATH", "HOME", "SHELL", "USER", "LOGNAME", "PWD", "OLDPWD", "TMPDIR", "TEMP", "TMP",
    "TERM", "TERMINFO", "COLORTERM", "COLUMNS", "LINES", "TZ", "LANG", "LANGUAGE", "LC_ALL",
    "EDITOR", "VISUAL", "PAGER", "DISPLAY", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "JAVA_HOME", "GRADLE_USER_HOME", "M2_HOME", "MAVEN_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT",
    "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOROOT", "GOMODCACHE", "GOCACHE", "GOBIN",
    "PUB_CACHE", "FLUTTER_ROOT", "NODE_PATH", "NVM_DIR", "PYENV_ROOT", "VIRTUAL_ENV",
    "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "GEM_HOME", "GEM_PATH", "HOMEBREW_PREFIX",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "CI",
}
_ENV_ALLOW_PREFIX = ("LC_", "GIT_", "XDG_")
_MAX_OUTPUT_BYTES = 2_000_000   # cap on captured child output — OOM guard vs `yes` / `cat /dev/urandom`


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() not in ("", "0", "no", "false", "off")


def _is_secret_env_name(name: str) -> bool:
    """A credential-shaped var NAME to drop by default. `*_FILE`/`*_PATH` are exempt:
    by convention they hold a path to a credential (e.g. AWS_WEB_IDENTITY_TOKEN_FILE),
    not the secret itself, and dropping them breaks legitimate auth/build flows."""
    if name.upper().endswith(("_FILE", "_PATH")):
        return False
    return bool(_ENV_SECRET_RE.search(name))


def _child_env():
    """The environment to hand a spawned command, or None to inherit the parent's as-is
    (TWOB_NO_ENV_SCRUB opt-out). Default drops credential-shaped names so ambient secrets
    can't be read by a command 2B runs; strict (TWOB_SEATBELT=strict) keeps only an
    allowlist of vars a shell/build legitimately needs."""
    if _env_flag("TWOB_NO_ENV_SCRUB"):
        return None
    src = os.environ
    if (os.environ.get("TWOB_SEATBELT") or "").strip().lower() == "strict":
        return {k: v for k, v in src.items() if k in _ENV_ALLOW or k.startswith(_ENV_ALLOW_PREFIX)}
    return {k: v for k, v in src.items() if not _is_secret_env_name(k)}


def _run_cancellable(cmd, *, shell, timeout, cancel, env=None):
    """Run a subprocess in its own process group, polling `cancel` (a threading.Event
    or None) so esc can kill it — and everything it spawned — within ~100ms instead of
    waiting out `timeout`. Output (stdout+stderr) is drained on a reader thread and
    capped at _MAX_OUTPUT_BYTES: past the cap we keep reading (so the child's pipe never
    wedges) but stop storing, then stop the runaway. `env`, when not None, replaces the
    child environment (see _child_env). Returns (returncode, combined_output, status)
    where status is 'ok' | 'timeout' | 'cancelled' | 'kill_failed'. Raises
    FileNotFoundError if the program isn't found (caller maps the message)."""
    if cancel is not None and cancel.is_set():   # already stopped — don't fork/exec
        return (None, "", "cancelled")
    proc = subprocess.Popen(
        cmd, shell=shell, cwd=os.getcwd(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True, env=env,
    )
    chunks, total, capped = [], [0], [False]

    def _drain():
        # Read to EOF so the pipe never fills (which would wedge the child); store only
        # up to the cap, then keep reading and discarding. This thread OWNS proc.stdout:
        # it closes it here when the read finishes. The main function must NOT close it —
        # close() blocks on the same buffer lock this blocking read() holds, so closing a
        # stream this thread is parked in would hang the caller whenever the child can't
        # be killed (root/sudo child, D-state I/O). Leaving ownership here lets the caller
        # return promptly with 'kill_failed' while this daemon cleans up when the child dies.
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                if total[0] < _MAX_OUTPUT_BYTES:
                    chunks.append(chunk)
                    total[0] += len(chunk)
                    if total[0] >= _MAX_OUTPUT_BYTES:
                        capped[0] = True
        except (ValueError, OSError):
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    start = time.monotonic()

    def _collect():
        reader.join(timeout=1.0)
        text = "".join(chunks)
        if capped[0]:
            text += f"\n… [output truncated: exceeded ~{_MAX_OUTPUT_BYTES // 1_000_000}MB cap] …"
        elif reader.is_alive():   # drain didn't finish in time — flag rather than imply complete
            text += "\n… [output may be incomplete] …"
        return text

    while True:
        if cancel is not None and cancel.is_set():
            return (None, "", "cancelled" if _killpg(proc) else "kill_failed")
        if timeout is not None and (time.monotonic() - start) > timeout:
            return (None, "", "timeout" if _killpg(proc) else "kill_failed")
        if capped[0]:                        # runaway output — stop it, keep what we have
            killed = _killpg(proc)
            out = _collect()
            rc = proc.poll()
            return (rc if rc is not None else 0, out, "ok" if killed else "kill_failed")
        try:
            proc.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            continue
        return (proc.returncode, _collect(), "ok")


_GIT_SHELL_OPS = {"&&", "||", "|", ";", "&", ">", ">>", "<", ">&", "&>", "|&"}


def has_shell_syntax(args: str) -> bool:
    """True if `git <args>` contains a shell operator (so the orchestrator can reject it
    before prompting, rather than confirm-then-fail)."""
    try:
        parts = shlex.split(args or "")
    except ValueError:
        return False
    return bool(parts) and _git_shell_syntax(parts)


def _git_shell_syntax(parts) -> bool:
    """True if the tokens contain a shell operator or redirection — the model tried to
    chain/pipe/redirect (e.g. 'add x && diff y'). run_git runs git directly (no shell),
    so these are passed to git literally and it just errors 128; catching them lets us
    return a clear, recoverable message instead. shlex keeps a quoted operator inside a
    single token, so a bare operator token is unambiguous — a commit message like
    'a && b' is one token and never trips this."""
    for p in parts:
        if p in _GIT_SHELL_OPS or re.match(r"^\d*[<>]", p):
            return True
    return False


def do_run_git(args, max_chars=None, cancel=None):
    """Run `git <args>` in the project — git only, never a shell (no chaining or
    injection). Confirmation/plan-mode gating happens in the orchestrator; this
    just executes. Output (stdout+stderr) is capped and non-zero exit is flagged.
    `cancel` (a threading.Event) lets esc kill a long-running git immediately."""
    try:
        parts = shlex.split(args or "")
    except ValueError as e:
        return f"error: could not parse git args: {e}"
    if not parts:
        return "error: no git command given"
    if _git_shell_syntax(parts):
        return ("error: run_git runs a single git command — no shell operators "
                "(&& || | ; > <). They aren't executed (this is git-only), so git rejects "
                "them. Make one run_git call per command, e.g. run_git \"add -A\" then "
                "run_git \"diff --cached\".")
    try:
        rc, out, status = _run_cancellable(["git", *parts], shell=False,
                                           timeout=GIT_TIMEOUT, cancel=cancel, env=_child_env())
    except FileNotFoundError:
        return "error: git is not installed"
    except Exception as e:
        return f"error: {e}"
    if status == "kill_failed":
        return f"error: tried to stop git {parts[0]} but it may still be running (could not signal its process group)"
    if status == "cancelled":
        return f"stopped: git {parts[0]} interrupted"
    if status == "timeout":
        return f"error: git {parts[0]} timed out after {GIT_TIMEOUT}s"
    out = out.strip() or f"(git {parts[0]}: no output)"
    if max_chars and len(out) > max_chars:
        head, tail = out[: max_chars * 2 // 3], out[-max_chars // 3:]
        out = f"{head}\n… [git output truncated] …\n{tail}"
    out = untrusted.wrap(out, f"run_git:{parts[0]}")   # repo content is untrusted (commit msgs, diffs)
    return f"error: git exited {rc}\n{out}" if rc else out


def do_run_command(command, max_chars=None, cancel=None, on_denied=None):
    """Run an arbitrary shell command in the project (cloud models only — see the
    orchestrator's model-aware tool exposure). Confirmation/plan gating happens
    upstream; this just executes. Output is capped and non-zero exit is flagged.
    `cancel` (a threading.Event) lets esc kill the command — and the whole process
    group it spawns (tests, builds) — immediately instead of blocking on timeout.

    When the workspace seatbelt is active (see seatbelt.mode), the command runs under
    `sandbox-exec` with writes confined to the project. `on_denied` (an optional
    callable → bool) is consulted only when a sandboxed command fails in a way that
    looks like a write denial: if it returns True the command is re-run once WITHOUT
    the sandbox. It's left None in unattended contexts so a denial simply stands
    (fail-closed — never silently widen the sandbox without a human)."""
    if not (command or "").strip():
        return "error: no command given"
    argv, _strict = seatbelt.wrap(command)
    sandboxed = argv is not None
    env = _child_env()
    try:
        rc, out, status = _run_cancellable(argv if sandboxed else command,
                                           shell=not sandboxed, timeout=CMD_TIMEOUT, cancel=cancel, env=env)
    except Exception as e:
        return f"error: {e}"
    denied = sandboxed and status == "ok" and bool(rc) and seatbelt.looks_like_denial(rc, out)
    # Sandbox blocked a write and a human chose to drop the sandbox for this one run.
    if denied and on_denied is not None and on_denied():
        denied = False
        try:
            rc, out, status = _run_cancellable(command, shell=True, timeout=CMD_TIMEOUT, cancel=cancel, env=env)
        except Exception as e:
            return f"error: {e}"
    if status == "kill_failed":
        return "error: tried to stop the command but it may still be running (could not signal its process group)"
    if status == "cancelled":
        return "stopped: command interrupted"
    if status == "timeout":
        return f"error: command timed out after {CMD_TIMEOUT}s"
    out = out.strip() or "(no output)"
    if max_chars and len(out) > max_chars:
        head, tail = out[: max_chars * 2 // 3], out[-max_chars // 3:]
        out = f"{head}\n… [output truncated] …\n{tail}"
    out = untrusted.wrap(out, "run_command")   # command output is untrusted (may carry injected text)
    if not rc:
        return out
    msg = f"error: command exited {rc}\n{out}"
    if denied:
        # Give the model a recoverable explanation so it stops retrying the same write.
        msg += ("\n(the workspace sandbox blocked a write outside the project — this ran write-confined. "
                "Write inside the project instead; the user can allow it, or set TWOB_NO_SEATBELT=1.)")
    return msg


# --- tool-call arg coercion (host-side robustness for small models) ----------
# A small model often emits a valid tool call in a malformed-but-recoverable
# shape: the arguments as a JSON *string*, the real args nested one level under
# an 'arguments'/'args' key, or the tool name present only *inside* that wrapper
# (with an empty or generic outer name). coerce_tool_args untangles those shapes
# before dispatch so a recoverable call isn't rejected. It never invents a name
# or arguments — an unrecognizable shape yields empty args, which the required-
# argument check then reports back to the model as a recoverable error.
_NAME_KEYS = ("name", "tool", "action", "tool_name", "function")
_ARG_KEYS = ("arguments", "args", "parameters", "params", "input", "tool_input")
# Only these frozen tools are ever unwrapped from a wrapper shape. Their parameter
# names never collide with a wrapper key, so unwrapping is unambiguous. run_git's
# own parameter is literally named 'args', run_command takes a raw 'command', and
# MCP tools carry arbitrary schemas that may legitimately have a sole 'input'/
# 'params' object — unwrapping any of those would silently send the wrong shape.
_UNWRAP_TOOLS = frozenset({"read_file", "edit_file", "write_file", "search_files", "list_files"})


def _as_arg_dict(value) -> dict:
    """A dict of arguments from `value`, re-parsing a stringified-JSON object.
    Anything that isn't (or doesn't parse to) an object becomes {}."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s:
            try:
                parsed = json.loads(s)
            except (ValueError, TypeError):
                return {}
            if isinstance(parsed, dict):
                return parsed
    return {}


def _unwrap_nested(args: dict) -> tuple[dict, str] | None:
    """If `args` is only a wrapper around the real arguments — an 'arguments'/'args'/
    'input' key plus at most a name key — return (inner_args, name_found_inside);
    else None. 'name_found_inside' is '' when the wrapper carries no name key. The
    'only a wrapper' guard keeps a genuine call that also carries one of these field
    names from being unwrapped by mistake."""
    for k in _ARG_KEYS:
        if k in args:
            inner = _as_arg_dict(args[k])
            extra = set(args) - {k} - set(_NAME_KEYS)
            if inner and not extra:
                wrapped_name = ""
                for nk in _NAME_KEYS:
                    v = args.get(nk)
                    if isinstance(v, str) and v.strip():
                        wrapped_name = v.strip()
                        break
                return inner, wrapped_name
    return None


def coerce_tool_args(name: str, args, known: tuple[str, ...] = ()) -> tuple[str, dict]:
    """Normalize a model's (name, arguments) into a clean (name, dict) before dispatch.

    Recovers the common small-model malformations for the frozen file tools: args as
    a JSON string, args nested under an 'arguments'/'args'/… key, or the tool name
    present only inside that wrapper (with an empty/unknown outer name). `known` is the
    set of currently-valid tool names; a name found inside a wrapper overrides an
    empty or unknown outer name. Only the frozen file tools are unwrapped (see
    _UNWRAP_TOOLS) — never run_git/run_command/MCP, whose own params could collide
    with a wrapper key. Pure and side-effect free.
    """
    name = (name or "").strip()
    args = _as_arg_dict(args)
    unwrapped = _unwrap_nested(args)
    if unwrapped is None:
        return name, args
    inner, wrapped_name = unwrapped
    eff_name = name
    if wrapped_name and (not name or (known and name not in known)):
        eff_name = wrapped_name
    if eff_name in _UNWRAP_TOOLS:
        return eff_name, inner
    return name, args


def command_arg_str(value) -> str:
    """The string form of a command tool's argument (run_git's 'args', run_command's
    'command'). Models sometimes emit it as a list of parts (['status'],
    ['commit', '-m', 'msg']); shlex.join reassembles it losslessly so do_run_git /
    do_run_command re-split it correctly. None -> ''; any other type -> str(). Keeps a
    list-valued arg from crashing a downstream .strip() and killing the whole task."""
    if isinstance(value, (list, tuple)):
        return shlex.join(str(v) for v in value)
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_PY_LITERALS = ((r"\bTrue\b", "true"), (r"\bFalse\b", "false"), (r"\bNone\b", "null"))
# A ```json … ``` (or ```tool_call …```) fenced block wrapping a JSON object/array.
_FENCE_RE = re.compile(
    r"```(?:json|tool_call|tool_calls)?\s*\n?(\{.*?\}|\[.*?\])\s*```",
    re.DOTALL | re.IGNORECASE,
)


def loads_tolerant(s: str):
    """json.loads with a conservative repair pass (trailing commas, Python literals
    True/False/None, one level of unclosed brace/bracket). Returns the parsed value or
    None. Never raises. Repairs only apply after a strict parse fails, so valid JSON is
    never altered. A cheap safety net for text-emitted tool-call blobs (see recover_toolcalls)."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass
    repaired = _TRAILING_COMMA_RE.sub(r"\1", s)
    for pat, repl in _PY_LITERALS:
        repaired = re.sub(pat, repl, repaired)
    for _ in range(3):
        try:
            return json.loads(repaired)
        except (ValueError, TypeError):
            opens = repaired.count("{") - repaired.count("}")
            brackets = repaired.count("[") - repaired.count("]")
            if opens <= 0 and brackets <= 0:
                return None
            repaired = repaired + ("}" if opens > 0 else "]")
    return None


def recover_toolcalls(text: str, known) -> list[tuple[str, dict]]:
    """Recover tool calls a model emitted as JSON in its message text instead of the native
    tool_calls field (measured: qwen2.5-coder:14b does this for 100% of its calls). Returns a
    list of (name, args) for each JSON object — in a fenced code block, or the whole message
    body — whose tool name (outer, or wrapped via coerce_tool_args) is in `known`; [] if none.
    Pure. Bare JSON embedded mid-prose is intentionally not scanned (too risky); it is still
    recovered when the whole body is JSON."""
    if not text or not any(k in text for k in known):
        return []
    calls: list[tuple[str, dict]] = []
    blobs = [m.group(1) for m in _FENCE_RE.finditer(text)]
    stripped = text.strip()
    if stripped[:1] in "{[":
        blobs.append(stripped)
    for blob in blobs:
        obj = loads_tolerant(blob)
        if obj is None:
            continue
        for item in (obj if isinstance(obj, list) else [obj]):
            if not isinstance(item, dict):
                continue
            name = ""
            for nk in _NAME_KEYS:
                v = item.get(nk)
                if isinstance(v, str) and v.strip():
                    name = v.strip()
                    break
            cname, cargs = coerce_tool_args(name, item, tuple(known))
            if cname in known:
                calls.append((cname, cargs))
    return calls
