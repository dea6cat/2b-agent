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

-- The compaction archive (P17): individual turns folded away by compaction are kept
-- here, searchable, so a later "that file you edited earlier" can be recalled without
-- carrying every turn in the live context. Lossy compaction stays; this is the durable
-- backstop behind it. `role`/`tool_name` are indexed for cheap filtered LIKE recall; the
-- full message is stored as JSON so a hit can be re-injected verbatim. No embeddings —
-- pure stdlib. (FTS5 + INSERT/DELETE/UPDATE triggers are the documented next tier if
-- substring recall ever proves too coarse; the LIKE floor ships first.)
CREATE TABLE IF NOT EXISTS archive (
  session_id   TEXT NOT NULL,
  cwd          TEXT NOT NULL,
  seq          INTEGER NOT NULL,     -- ordinal within (session_id, cwd), for stable ordering
  role         TEXT,                 -- 'user' | 'assistant'
  tool_name    TEXT,                 -- tool called (assistant) or answered (result turn); '' otherwise
  text         TEXT,                 -- flattened searchable text of the turn
  message_json TEXT NOT NULL,        -- full serialized Message, for verbatim re-injection
  created_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_archive_key ON archive(session_id, cwd, seq);
CREATE INDEX IF NOT EXISTS idx_archive_role ON archive(role);
CREATE INDEX IF NOT EXISTS idx_archive_tool ON archive(tool_name);
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


# --- compaction archive (P17) -----------------------------------------------
# Turns dropped by compaction are archived here so they can be recalled on demand,
# instead of being lost the moment they fall out of the live context.

_ARCHIVE_TEXT_CAP = 4000        # per-turn stored text ceiling — recall needs a fingerprint, not the whole body


def _turn_tool_name(m, call_names: dict) -> str:
    """The tool a turn is about: the (first) call an assistant made, or the tool a
    result turn answers (looked up by call id via `call_names`). '' when neither."""
    if m.tool_calls:
        return m.tool_calls[0].name or ""
    for r in m.tool_results:
        name = call_names.get(r.tool_call_id)
        if name:
            return name
    return ""


def _capped_message_dict(m) -> dict:
    """A serializable Message dict with every large text field bounded to _ARCHIVE_TEXT_CAP,
    so the stored JSON (which is re-injected verbatim on recall) can't hold a full file-read
    or long command output forever. Recall wants a fingerprint of the turn, not its full body."""
    d = _conv.message_to_dict(m)
    if d.get("text"):
        d["text"] = d["text"][:_ARCHIVE_TEXT_CAP]
    if d.get("thinking"):
        d["thinking"] = d["thinking"][:_ARCHIVE_TEXT_CAP]
    for c in d.get("tool_calls") or []:
        args = c.get("arguments")
        if isinstance(args, dict):
            for k, v in list(args.items()):
                if isinstance(v, str) and len(v) > _ARCHIVE_TEXT_CAP:
                    args[k] = v[:_ARCHIVE_TEXT_CAP]
    for r in d.get("tool_results") or []:
        if r.get("content"):
            r["content"] = r["content"][:_ARCHIVE_TEXT_CAP]
    return d


def _searchable_text(m) -> str:
    """Flatten one Message to the text worth searching: user/assistant prose, the
    assistant's tool calls with their arguments, and any tool-result bodies."""
    parts: list[str] = []
    if m.text:
        parts.append(m.text)
    if m.thinking:
        parts.append(m.thinking)
    for c in m.tool_calls:
        parts.append(f"{c.name} {c.arguments}")
    for r in m.tool_results:
        if r.content:
            parts.append(r.content)
    return "\n".join(parts)[:_ARCHIVE_TEXT_CAP]


def archive_messages(session_id: str, cwd: str, messages) -> None:
    """Append `messages` (turns being folded away by compaction) to the searchable
    archive, in order, after whatever is already stored for this (session, cwd).
    Skips empty turns and prior recap summaries. Best-effort — never raises."""
    if not enabled() or not messages:
        return
    try:
        abscwd = os.path.abspath(cwd or ".")
        now = time.time()
        call_names: dict[str, str] = {}
        rows = []
        for m in messages:
            # Track call id -> tool name so a following result turn can be labeled.
            for c in m.tool_calls:
                call_names[c.id] = c.name
            text = _searchable_text(m)
            if not text.strip():
                continue                      # nothing to recall from an empty turn
            rows.append((
                m.role.value,
                _turn_tool_name(m, call_names),
                text,
                json.dumps(_capped_message_dict(m)),
            ))
        if not rows:
            return
        with _db() as c:
            base = c.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM archive WHERE session_id=? AND cwd=?",
                             (session_id, abscwd)).fetchone()[0]
            c.executemany(
                "INSERT INTO archive(session_id, cwd, seq, role, tool_name, text, message_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(session_id, abscwd, base + i, role, tool, text, mj, now)
                 for i, (role, tool, text, mj) in enumerate(rows)],
            )
    except Exception as e:
        _debug(f"archive_messages({session_id}) failed: {e}")


def search_archive(session_id: str, cwd: str, terms, limit: int = 3) -> list[dict]:
    """Recall archived turns for this (session, cwd) whose text matches `terms`, ranked by
    how many distinct terms hit (then most-recent-first). Returns dicts with the rebuilt
    `message` plus role/tool_name/seq. A host-side LIKE search — no embeddings. Best-effort."""
    terms = [t for t in (terms or []) if t and len(t) >= 2][:8]
    if not enabled() or not terms:
        return []
    try:
        abscwd = os.path.abspath(cwd or ".")
        # Score = count of matched terms; each term is an escaped, case-insensitive LIKE.
        # ESCAPE '\' so a term containing % or _ can't turn into a wildcard.
        like = "text LIKE ? ESCAPE '\\'"
        score = " + ".join([f"({like})"] * len(terms))
        where = " OR ".join([like] * len(terms))
        like_args = [f"%{_like_escape(t)}%" for t in terms]
        sql = (
            f"SELECT role, tool_name, message_json, seq, ({score}) AS hits "
            f"FROM archive WHERE session_id=? AND cwd=? AND ({where}) "
            "ORDER BY hits DESC, seq DESC LIMIT ?"
        )
        with _db() as c:
            rows = c.execute(sql, like_args + [session_id, abscwd] + like_args + [limit]).fetchall()
        out = []
        for role, tool_name, mj, seq, hits in rows:
            if not hits:
                continue
            try:
                msg = _conv.message_from_dict(json.loads(mj))
            except Exception:
                continue
            out.append({"role": role, "tool_name": tool_name, "seq": seq, "hits": hits, "message": msg})
        return out
    except Exception as e:
        _debug(f"search_archive({session_id}) failed: {e}")
        return []


def _like_escape(term: str) -> str:
    """Escape LIKE wildcards so a search term is matched literally (ESCAPE '\\')."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
