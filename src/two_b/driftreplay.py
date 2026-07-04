"""Prompt-drift replay (P10). Each saved session records a salted hash of the assembled
prefix (the system prompt the model actually saw). `2b trace replay <session>` rebuilds the
prefix with the CURRENT code and project files and reports whether it drifted — a cheap,
no-LLM way to tell whether a code change or an edited CLAUDE.md/project map would now feed a
recorded session a different prefix, which is the usual hidden cause of "it behaves differently
than the transcript shows."

Pure and stdlib-only. 2B's prefix is byte-stable across a session's turns (P5), so drift is a
per-session property, not per-turn — the check is one comparison per session.
"""
from __future__ import annotations

import hashlib

# Versions the hashing scheme. Bumping it deliberately invalidates old hashes (e.g. if the
# prefix assembly changes shape) so a stale hash can't read as a spurious match.
_SALT = "2b-prefix-v1"


def prefix_hash(system_prompt: str) -> str:
    """A salted SHA-256 of the assembled prefix. Short hex — enough to spot a change, not a
    cryptographic commitment (the prefix isn't secret)."""
    h = hashlib.sha256()
    h.update(_SALT.encode())
    h.update(b"\x00")
    h.update((system_prompt or "").encode("utf-8", errors="replace"))
    return h.hexdigest()[:16]


def drift(stored_hash: str | None, current_system_prompt: str) -> dict:
    """Compare a session's stored prefix hash against the current-code prefix. Returns
    {drift, stored, current, reason}. `drift` is None (unknown) when no hash was stored —
    an older session recorded before P10, which can't be compared, not a real match."""
    if not stored_hash:
        return {"drift": None, "stored": None, "current": prefix_hash(current_system_prompt),
                "reason": "no prefix hash recorded for this session (predates drift tracking)"}
    current = prefix_hash(current_system_prompt)
    changed = current != stored_hash
    return {"drift": changed, "stored": stored_hash, "current": current,
            "reason": "prefix changed since this session was recorded" if changed
                      else "prefix identical to when recorded"}


def replay(session_id: str, cwd: str | None = None) -> dict:
    """Rebuild a recorded session's prefix with the CURRENT code + project files and report
    drift. Returns {found, ...meta, ...drift}. No model call. Lazy imports keep this cheap.
    Scoped to `cwd` when given (session ids are only 8 hex chars — a bare id can collide
    across projects, so replay resolves within one project like load() does)."""
    import os
    from . import persist, orchestrator
    meta = persist.get_meta(session_id, cwd=cwd or os.getcwd())
    if meta is None:
        return {"found": False, "id": session_id}
    current_prefix = orchestrator.assemble_system_prompt(cwd=meta.get("cwd"))
    out = {"found": True, **meta, **drift(meta.get("prefix_hash"), current_prefix)}
    return out


def trace_main(argv) -> int:
    """`2b trace replay <session_id>` — report whether a recorded session's prefix drifted."""
    import argparse
    ap = argparse.ArgumentParser(prog="2b trace",
                                 description="Inspect recorded sessions (no model call).")
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("replay", help="Report prefix drift for a recorded session")
    rp.add_argument("session_id", help="Session id (see 2b --list-sessions or /sessions)")
    args = ap.parse_args(argv)
    if args.cmd != "replay":
        ap.print_help()
        return 2
    r = replay(args.session_id)
    if not r["found"]:
        print(f"error: no saved session '{args.session_id}' (see 2b --list-sessions)")
        return 1
    title = r.get("title") or "(untitled)"
    print(f"session {r['id']}  {title}")
    print(f"  cwd:     {r.get('cwd')}")
    print(f"  model:   {r.get('model')}")
    print(f"  stored:  {r.get('stored') or '—'}")
    print(f"  current: {r.get('current')}")
    if r["drift"] is None:
        print(f"  drift:   unknown — {r['reason']}")
        return 0
    print(f"  drift:   {'true' if r['drift'] else 'false'} — {r['reason']}")
    return 0
