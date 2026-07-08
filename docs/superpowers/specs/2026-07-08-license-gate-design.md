# First-run license acknowledgment gate

**Date:** 2026-07-08
**Status:** Approved, pending implementation
**Branch:** `license/polyform-noncommercial` (builds on the PolyForm relicense)

## Problem / goal

2B is now licensed under **PolyForm Noncommercial 1.0.0** (source-available, noncommercial
only; commercial use needs a separate license). We want users to **explicitly acknowledge**
the license before using the tool — primarily so commercial users are made aware they need
a license — across all three install channels (brew, curl, pip).

This is acknowledgment/deterrence + legal assent, **not** technical prevention: the source
is public and the gate is our own code, so it can be bypassed by editing it. Its value is a
clear, recorded "I agree" (clickwrap strengthens enforceability) and a visible commercial-use
notice.

## Constraints

- Homebrew's `brew install` runs non-interactively and cannot host an install-time prompt, so
  the gate must live in the **running tool** (first run), which is what makes one gate cover
  all three channels.
- Ask **once**: persist acceptance and never re-prompt unless the license changes or config is
  wiped.
- Do not block metadata/maintenance commands behind the prompt.

## Design

### New module `src/two_b/license.py`

Single responsibility: the acknowledgment gate.

- `LICENSE_ID = "PolyForm-Noncommercial-1.0.0"`
- `LICENSE_URL = "https://polyformproject.org/licenses/noncommercial/1.0.0"`
- A short notice constant (name · noncommercial-only · commercial-needs-a-license + where to
  ask · link).
- `accepted() -> bool` → `config.get_prefs().get("license_accepted") == LICENSE_ID`
- `record() -> None` → `config.set_pref("license_accepted", LICENSE_ID)`
- `ensure_accepted(*, assume_yes: bool, interactive: bool, out: Callable[[str], None]) -> bool`

`out` is an injected print function (so tests capture output and the caller controls
formatting — `cli.main` passes `console.print`, `setup` its own printer).

### Gate logic (`ensure_accepted`)

1. `accepted()` → return `True` immediately, print nothing (genuinely once).
2. Print the notice.
3. `assume_yes` → `record()`, return `True`. (Covers `install.sh`'s `2b setup --yes` and `2b --yes`.)
4. `interactive` (TTY) → prompt `Accept the PolyForm Noncommercial license and use 2B? [y/N] `.
   - Input `y`/`yes` (case-insensitive) → `record()`, return `True`.
   - Anything else (incl. bare Enter) → print "License not accepted — 2B will not run." → return `False`.
   - `EOFError` → treated as decline → `False`.
5. Non-interactive without `--yes` → print "Non-interactive: pass --yes to accept the license
   (see LICENSE / the URL above)." → return `False`.

**Decisions:** interactive default is **No** (must type `y` — affirmative assent).
Non-interactive acceptance is **`--yes`** (no new flag; documented that `--yes` implies license
acceptance). `--yes` already exists for auto-applying edits and is what `install.sh` passes.

### Wiring — two call sites

Both call the same idempotent gate; once accepted, neither re-prompts.

1. **`setup.main()`** — at the very top, before onboarding work. `assume_yes` = setup's existing
   yes flag; `interactive` = stdin is a TTY. If it returns `False`, abort setup with a nonzero
   exit. Covers `2b setup` (the curl/brew first-run funnel).
2. **`cli.main()`** — immediately before the agent-run section (after the early-exit subcommand
   blocks at `cli.py:300-389`, before model resolution at ~391). `assume_yes = args.yes`;
   `interactive = sys.stdin.isatty()`. If `False`, `raise SystemExit(1)`. Covers `2b` / `2b <task>`
   for users who skip setup (brew/pip).

**Not gated** (all early-exit before the call site): `--version`, `--help`, `--doctor`, `--rm`,
`--update`, `--list-models`, `--list-sessions`, `--print-ctx`. The gate fires only when actually
using the agent or running setup.

### Persistence

`prefs.json` key `license_accepted` = the accepted `LICENSE_ID` string. Re-prompts only if the
stored value differs from the current `LICENSE_ID` (future relicense) or config is wiped
(`--rm` / fresh install). Uses the existing `config.get_prefs()/set_pref()`.

### install.sh

Already hands off to `2b setup` (interactive via `/dev/tty`, else `2b setup --yes`), so
acceptance flows through the setup gate. Add a short one-line license notice to the script for
visibility; the real enforcement stays in the tool (so brew/pip are covered identically).

### Message (concise, not the full text)

> 2B is licensed under the PolyForm Noncommercial License 1.0.0.
> Free for noncommercial use (personal, hobby, research, education, nonprofits, government).
> Commercial use requires a separate license — open an issue at
> https://github.com/dea6cat/2b-agent to arrange one.
> Full terms: LICENSE / https://polyformproject.org/licenses/noncommercial/1.0.0

## Testing (`tests/test_license_gate.py`, unittest, tmp prefs)

Point `config.PREFS_FILE`/`CONFIG_DIR` at a tempdir per test.

- `accepted()`/`record()` roundtrip: false before, true after `record()`.
- `ensure_accepted(assume_yes=True, ...)` → returns True, records, prints notice, does not prompt.
- Already-accepted → returns True, prints nothing, does not prompt.
- Interactive decline (monkeypatch `input` → "" and "n") → returns False, does **not** record.
- Interactive accept (`input` → "y") → returns True, records.
- Non-interactive without yes (`interactive=False, assume_yes=False`) → returns False, does not record.
- Stored id mismatch (prefs has an old id) → not accepted → prompts again.

## Out of scope (YAGNI)

- No dedicated `--accept-license` flag (honor `--yes`).
- No gating of metadata/maintenance commands.
- No full-license-text pager; a concise notice + link is enough.
- No attempt at technical enforcement beyond the persisted flag.
