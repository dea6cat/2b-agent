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
- `ensure_accepted(*, assume_yes: bool, interactive: bool, out: Callable[[str], None], on_decline: Callable[[], None] | None = None) -> bool`
  - Validated on **every run** (cheap prefs read); once accepted it returns True silently, so it never nags.

`out` is an injected print function (so tests capture output and the caller controls
formatting — `cli.main` passes `console.print`, `setup` its own printer).

### Gate logic (`ensure_accepted`)

1. `accepted()` → return `True` immediately, print nothing (genuinely once).
2. Print the notice.
3. `assume_yes` → `record()`, return `True`. (Covers `install.sh`'s `2b setup --yes` and `2b --yes`.)
4. `interactive` (TTY) → prompt `Use 2B under this license?  [y] accept · [n] uninstall 2B · [Enter] cancel: `.
   - `y`/`yes` (case-insensitive) → `record()`, return `True`.
   - `n`/`no` → **explicit decline**: print "License declined — removing 2B.", call `on_decline()`
     (which runs the uninstall), return `False`.
   - Anything else, bare Enter, or `EOFError` → **cancel**: print "Not accepted — exiting without
     changes. You'll be asked again next run.", return `False`. Does **not** uninstall.
5. Non-interactive without `--yes` → print the "pass --yes" hint, return `False`. Does **not**
   uninstall (a stray CI/scripted run must never remove the tool).

**Decisions:**
- The gate is **validated on every run** (including the chat/TUI, via the `cli.main` gate that
  precedes launch). Once accepted it's silent; if the flag is missing/changed it prompts again.
- **Enter cancels** (safe): only an explicit `n`/`no` uninstalls, so a stray keypress can't wipe
  an install. Chosen over "default No + decline uninstalls," which would make bare Enter destructive.
- **Explicit decline → `--rm`**: `on_decline` runs `uninstall.run(emit, lambda _p: True,
  assume_yes=True)` (removes the tool + deletes `~/.config/2b`, no second prompt — the `n` is the
  consent), then exits via `SystemExit(uninstall's code)`.
- Non-interactive acceptance is **`--yes`** (no new flag; `--yes` implies acceptance and is what
  `install.sh` passes). Non-interactive never uninstalls.
- `on_decline` is injected so the pure gate stays testable (tests pass a recorder; real callers
  pass the uninstall).

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
- `ensure_accepted(assume_yes=True, ...)` → returns True, records, does not prompt, `on_decline` not called.
- Already-accepted → returns True, prints nothing, does not prompt.
- Interactive accept (`input` → "y") → returns True, records, `on_decline` not called.
- Explicit decline (`input` → "n") → returns False, does **not** record, `on_decline` called once.
- Enter (`input` → "") → returns False, does not record, `on_decline` **not** called (cancel).
- Unrecognized answer (`input` → "maybe") → returns False, `on_decline` not called (cancel).
- Non-interactive without yes → returns False, does not record, `on_decline` **not** called.
- Stored id mismatch (prefs has an old id) → not accepted → prompts again.

## Out of scope (YAGNI)

- No dedicated `--accept-license` flag (honor `--yes`).
- No gating of metadata/maintenance commands.
- No full-license-text pager; a concise notice + link is enough.
- No attempt at technical enforcement beyond the persisted flag.
