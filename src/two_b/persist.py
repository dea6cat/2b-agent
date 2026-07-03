"""Session persistence — save each task's conversation so it can be listed and
resumed later. Local-first and dependency-free: stdlib sqlite3, one database at
~/.config/2b/history.db (override with TWOB_HISTORY_DB; disable with
TWOB_NO_HISTORY). All operations are best-effort — persistence must never break a
task, so every call swallows its own errors and degrades to a no-op.

A "session" here is one task's conversation thread, keyed by the task id and
scoped to the project (cwd), so `--continue` resumes the most recent thread in
this directory.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time

from . import conversation as _conv

# (id, cwd) is the key, not id alone: task ids are only 8 hex chars, so a bare id
# key could let the same id in two projects clobber each other. Scoping by cwd also
# lets load() refuse an id from a different project.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT NOT NULL,
  cwd           TEXT NOT NULL,
  title         TEXT,
  model         TEXT,
  created_at    REAL,
  updated_at    REAL,
  messages_json TEXT NOT NULL,
  PRIMARY KEY (id, cwd)
);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd ON sessions(cwd, updated_at DESC);
"""

_initialized: set[str] = set()


def enabled() -> bool:
    return not os.environ.get("TWOB_NO_HISTORY")


def _debug(msg: str) -> None:
    if os.environ.get("TWOB_DEBUG"):
        import sys
        print(f"[2b persist] {msg}", file=sys.stderr)


def _db_path() -> str:
    override = os.environ.get("TWOB_HISTORY_DB")
    if override:
        os.makedirs(os.path.dirname(os.path.abspath(override)) or ".", exist_ok=True)
        return override
    d = os.path.expanduser("~/.config/2b")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "history.db")


@contextlib.contextmanager
def _db():
    """A connection that commits on clean exit and always closes (sqlite3's own
    context manager handles the transaction but leaves the connection open). Schema
    creation runs once per DB path per process, off the hot path."""
    path = _db_path()
    c = sqlite3.connect(path, timeout=5)
    try:
        if path not in _initialized:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)
            _initialized.add(path)
        yield c
        c.commit()
    finally:
        c.close()


def save(session_id: str, cwd: str, title: str, model: str, conversation) -> None:
    """Insert or update a session's conversation. Skips trivial conversations (a
    system prompt with no real turns). Best-effort."""
    if not enabled() or conversation is None:
        return
    # Nothing worth saving until there's a real exchange (system prompt lives in a
    # separate field, so messages are only user/assistant/tool-result turns).
    if len(conversation.messages) < 2:
        return
    try:
        payload = json.dumps(_conv.to_jsonable(conversation))
        now = time.time()
        abscwd = os.path.abspath(cwd or ".")
        with _db() as c:
            row = c.execute("SELECT created_at FROM sessions WHERE id=? AND cwd=?",
                            (session_id, abscwd)).fetchone()
            created = row[0] if row else now
            c.execute(
                "INSERT OR REPLACE INTO sessions"
                "(id, cwd, title, model, created_at, updated_at, messages_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, abscwd, title, model, created, now, payload),
            )
    except Exception as e:
        _debug(f"save({session_id}) failed: {e}")


def relative_age(ts: float, now: float | None = None) -> str:
    """A short human age ('just now', '5m ago', '2h ago', '3d ago') for a unix time."""
    now = time.time() if now is None else now
    d = max(0, int(now - (ts or 0)))
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def list_sessions(cwd: str | None = None, limit: int = 20) -> list[dict]:
    """Most-recent-first session summaries (id, title, model, updated_at, messages),
    optionally scoped to a project dir. `messages` is the turn count in the thread."""
    if not enabled():
        return []
    try:
        with _db() as c:
            if cwd:
                rows = c.execute(
                    "SELECT id, title, model, updated_at, messages_json FROM sessions "
                    "WHERE cwd=? ORDER BY updated_at DESC LIMIT ?",
                    (os.path.abspath(cwd), limit)).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, title, model, updated_at, messages_json FROM sessions "
                    "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            try:
                n = len(json.loads(r[4]).get("messages", []))
            except Exception:
                n = 0
            out.append({"id": r[0], "title": r[1], "model": r[2], "updated_at": r[3], "messages": n})
        return out
    except Exception as e:
        _debug(f"list_sessions failed: {e}")
        return []


def load(session_id: str, cwd: str | None = None):
    """Rebuild the Conversation for a session id, or None if unknown/unreadable. When
    `cwd` is given, the id must belong to that project — so `--resume <id>` can't
    silently attach another project's history to this directory."""
    if not enabled():
        return None
    try:
        with _db() as c:
            if cwd:
                row = c.execute("SELECT messages_json FROM sessions WHERE id=? AND cwd=?",
                                (session_id, os.path.abspath(cwd))).fetchone()
            else:
                row = c.execute("SELECT messages_json FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        return _conv.from_jsonable(json.loads(row[0]))
    except Exception as e:
        _debug(f"load({session_id}) failed: {e}")
        return None


def most_recent_id(cwd: str) -> str | None:
    """The id of the latest session in this project dir, for `--continue`."""
    rows = list_sessions(cwd=cwd, limit=1)
    return rows[0]["id"] if rows else None
