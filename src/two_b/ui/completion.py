"""Pure input-completion helpers for the TUI (Phase 4.4): the @-file token being
typed, and ranking project files against a partial. Dependency-free (no rich/textual)
so it's unit-testable; app_tui supplies the file list and wires it into the palette.
"""
import os


def at_token(text: str):
    """The file partial being typed after the LAST '@' in `text`, when that token is
    whitespace-free (i.e. still being typed), else None. '' means '@' was just typed.
    So 'edit @lib/foo' -> 'lib/foo'; 'edit @a b' -> None (the token already ended)."""
    idx = text.rfind("@")
    if idx == -1:
        return None
    tok = text[idx + 1:]
    if any(c.isspace() for c in tok):
        return None
    return tok


def rank_files(files, partial, limit=8):
    """Rank project relpaths for an @-partial: basename-prefix matches first, then
    path-prefix, then substring; each group preserves input order (so a caller that
    pre-sorts shortest-first keeps near-root files on top). Case-insensitive. An empty
    partial returns the first `limit` files."""
    if not partial:
        return list(files[:limit])
    p = partial.lower()
    base_pref, path_pref, sub = [], [], []
    for f in files:
        fl = f.lower()
        if os.path.basename(fl).startswith(p):
            base_pref.append(f)
        elif fl.startswith(p):
            path_pref.append(f)
        elif p in fl:
            sub.append(f)
    return (base_pref + path_pref + sub)[:limit]
