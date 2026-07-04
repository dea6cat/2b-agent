"""Durable undo log — mirrors a task's pre-edit snapshots to disk so /undo survives a
restart or a `--continue`/`--resume`.

The in-memory Task.edit_history stays the live undo stack (a list of (path, pre) where
pre is the file's content before the edit, or None for a newly-created file). This
module persists that list after every change and restores it when a task resumes, so an
edit made in a previous session can still be undone.

One JSON file per (task, project), written atomically (os.replace). Snapshots (the pre
content) are stored inline, size-capped so one huge file can't bloat the log. Best-effort
throughout — it must never break a task, so every call swallows its own errors and
degrades to a no-op. Disable with TWOB_NO_HISTORY; relocate with TWOB_UNDO_DIR.

2B's frozen tools only create and modify files (edit_file/write_file), so those are the
operations logged; there is no delete/rename tool to snapshot.
"""
import hashlib
import json
import os
import threading
import time

MAX_ENTRIES = 50                  # matches Task.push_edit's in-memory cap
MAX_SNAPSHOT_BYTES = 1_000_000    # don't persist a pre-image larger than this
_PRUNE_AGE = 30 * 86400           # sweep undo logs untouched for 30 days (bounds growth)


def enabled() -> bool:
    return not os.environ.get("TWOB_NO_HISTORY")


def _dir() -> str:
    d = os.environ.get("TWOB_UNDO_DIR") or os.path.join(os.path.expanduser("~/.config/2b"), "undo")
    os.makedirs(d, exist_ok=True)
    return d


def _path(task_id: str, cwd: str) -> str:
    key = hashlib.sha1(os.path.abspath(cwd or ".").encode("utf-8", "replace")).hexdigest()[:12]
    return os.path.join(_dir(), f"{key}-{task_id}.json")


def save(task_id: str, cwd: str, edit_history) -> None:
    """Rewrite the on-disk undo log to mirror `edit_history` [(path, pre_or_None), …],
    atomically. An entry whose pre-image is too large to persist is marked oversize (its
    content is dropped) so a later restore skips it rather than restoring wrong content."""
    if not enabled():
        return
    tmp = None
    try:
        entries = []
        for path, pre in list(edit_history)[-MAX_ENTRIES:]:
            if pre is not None and len(pre) > MAX_SNAPSHOT_BYTES:
                entries.append({"path": path, "oversize": True})
            else:
                entries.append({"path": path, "pre": pre})
        p = _path(task_id, cwd)
        # Disambiguate the temp file by process AND thread: a backgrounded task's worker
        # and a /undo on the same task id can both save concurrently; a per-thread temp
        # avoids one truncating the other's in-flight write (os.replace stays atomic).
        tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w") as f:
            json.dump({"entries": entries}, f)
        os.replace(tmp, p)
        tmp = None
        _prune()
    except Exception:
        if tmp:
            try:
                os.remove(tmp)      # don't leave an orphaned temp on a mid-write failure
            except OSError:
                pass


def load(task_id: str, cwd: str) -> list:
    """Restore [(path, pre_or_None), …] for a resumed task, or [] if none/unreadable.
    Oversize entries (content wasn't snapshotted) are dropped — undoing a create-revert
    on them could delete a file we can't restore, so they're better left non-undoable.
    Fully guarded (a malformed or wrong-shaped file yields [], never an exception) so it
    can never break task creation on resume."""
    if not enabled():
        return []
    try:
        with open(_path(task_id, cwd)) as f:
            data = json.load(f)
        entries = data.get("entries", []) if isinstance(data, dict) else []
        out = []
        for e in entries:
            if not isinstance(e, dict) or e.get("oversize"):
                continue
            out.append((e.get("path"), e.get("pre")))
        return out
    except Exception:
        return []


def _prune() -> None:
    """Delete undo logs (and stray temp files) untouched for _PRUNE_AGE, so the store
    stays bounded instead of keeping a small file per task forever. Best-effort."""
    try:
        d, now = _dir(), time.time()
        for name in os.listdir(d):
            if not (name.endswith(".json") or name.endswith(".tmp")):
                continue
            fp = os.path.join(d, name)
            try:
                if now - os.path.getmtime(fp) > _PRUNE_AGE:
                    os.remove(fp)
            except OSError:
                pass
    except Exception:
        pass
