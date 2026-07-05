# Roadmap execution handoff

Working state for resuming the `docs/performance-reliability-roadmap.md` execution in a
fresh session. Last updated after the full security theme shipped and P18 was closed.

## Where things stand

- **Branch/commit:** `main` at `940f6bc` (`local == origin`). Working tree clean.
- **Tests:** full suite **544 green** — `python -m unittest discover -s tests -p "test_*.py"`
  (5 Linux-gated bwrap behavioral tests skip on macOS).
- **Shipped on `main`:** P1–P11, P13–P17, P20, P22, P23, P25, P27 (perf/reliability roadmap),
  **plus the complete security theme: P12 (macOS seatbelt write-confinement) + command-approval,
  S2 (subprocess env scrub + bounded output), prompt-injection fencing, Linux write-confinement
  (bwrap), and the read-confinement tier** — see `docs/sandbox-capabilities-roadmap.md`,
  `docs/security-hardening-roadmap.md`, and the `2b-run-command-sandbox` memory. Each landed as
  one feature commit, adversarially reviewed (codeObserver) and validated.

### What each recent cluster added (one line each)
- **Read-confinement tier** (`940f6bc`): `TWOB_SEATBELT=strict` now also confines READS (macOS SBPL
  deny-read + allowlist; Linux bwrap binds only system dirs) so $HOME secrets are unreadable; +
  read-only tmpfs for missing protected paths. macOS device-validated; Linux unit-tested (CI caveat).
- **Linux write-confinement / bwrap** (`742083c`): `seatbelt.py` dispatches macOS `sandbox-exec` /
  Linux `bwrap`; write-confinement parity, `strict`→`--unshare-net`. Unit-tested; needs Linux CI.
- **Prompt-injection fencing** (`393d5ae`): `untrusted.py` fences env-derived tool output in
  `<untrusted_data>` markers + system-prompt data-not-instructions rule + forge-escaping.
- **S2** (`a315a84`): subprocess env scrub (`tools._child_env`) + bounded child output + `~/.config/2b`
  sensitive.
- **P12 + command-approval** (`c03b89e`): `seatbelt.py` macOS write-confinement; `cmdguard` folds
  network/escalation/secrets-path into `is_high_risk`; file tools/`search_files`/`run_git` confirm on a
  symlink-resolved secrets path.
- **P19+P10+P11** (`527777c`): `/tool` direct tool invocation, `/history search` scrollback
- **S2** (`a315a84`): subprocess env scrub (`tools._child_env` — denylist default / allowlist under
  `TWOB_SEATBELT=strict` / `TWOB_NO_ENV_SCRUB` opt-out; applies to run_command AND run_git) +
  bounded child output (~2MB reader-thread cap, OOM guard) + `~/.config/2b` as a sensitive path.
- **P12 + command-approval** (`c03b89e`, docs `d1b2646`): `seatbelt.py` macOS `sandbox-exec`
  write-confinement for run_command (permissive-base + deny-writes, on by default,
  `TWOB_NO_SEATBELT`/`TWOB_SEATBELT=strict`, deny→ask-to-re-run); `cmdguard` folds network/
  escalation/secrets-path into `is_high_risk`; file tools + `search_files` + `run_git` confirm on
  a symlink-resolved secrets path (`orchestrator._is_sensitive`).
- **P19+P10+P11** (`527777c`): `/tool` direct tool invocation, `/history search` scrollback
  nav, risk-class label on confirmations; `2b trace replay <id>` prompt-drift detection
  (salted prefix hash per session + `driftreplay.py`); TUI Esc-snap-to-bottom + double-Ctrl-C.
- **P8+P9** (`d791ee0`): project-instructions injection (CLAUDE.md/AGENTS.md → system prompt);
  eval rigor — `evalstats.py` (exact-match scorer, seeded bootstrap CIs, McNemar, multi-seed
  variance, dirty-tree publish guard), harness multi-seed + trace copy-out + `TWOB_EVAL_CLI`.
- **P17** (`89f2b1c`): searchable compaction archive behind lossy compaction + dangling-ref recall.
- **P5/P6/P22/P27** (`19cc21d`): prefix stability + keep-alive, token calibration, structured
  compaction summary.

## Remaining work

- **P18 — Scoped-prompt + bounded state: CLOSED (satisfied by design, not built).** Investigated
  per the "check before proposing" note: `subagents.py` already embodies P18's core idea — each
  explorer/worker runs in a **fresh `Conversation`** with only a short system prompt + the single
  `goal` (never the parent's history), is **bounded** (≤8/≤12 turns, `MAX_PARALLEL=4`,
  `DELEGATE_TIMEOUT=180s`), reads are `read_cap`-capped, the report folded back is truncated, and
  delegate is **cloud-only** (big context). Both preconditions for P18's value are absent (no
  full-history carry; no unbounded per-subagent growth), so a `state.py` ring-buffer would be
  speculative complexity against the YAGNI thesis. Reopen only if delegate gains a local-model path
  or long-lived stateful sub-runners.
- **Linux CI behavioral validation** of the bwrap write- and read-confinement paths (the 5 gated
  tests + a userns-enabled smoke check). The only outstanding item — needs a Linux environment;
  everything else is validated on macOS. See the `2b-run-command-sandbox` memory for specifics.

The roadmap's phased backlog is otherwise complete. Optional future ideas live in
`docs/sandbox-capabilities-roadmap.md` (TUI polish, per-model catalog extras) and
`docs/security-hardening-roadmap.md` (path-scoped grants, the S1 read-side jail — deliberately
deferred as it reverses point-anywhere).
- **Security follow-ons** (`docs/security-hardening-roadmap.md`, §D). Mostly shipped via P12+S2.
  Still open: **S1 read-side workspace jail** — deliberately NOT done because it reverses 2B's
  documented "point-anywhere" file access; S7 (shipped) already confirms *secrets* reads, so a
  blanket read-jail needs an explicit `AskUserQuestion` before starting. Lower-priority items
  (path-scoped grants, TUI polish from `docs/sandbox-capabilities-roadmap.md`) remain optional.

There are also documented *reinforcements* folded into shipped phases (see the roadmap's
"Reference implementations & reinforcements" section) — not separate phases.

## The per-phase process (follow exactly — the user relies on it)

1. **Branch off `main` first.** `git fetch origin && git checkout -b feat/<name> origin/main`.
   NEVER commit phase work directly to `main` (a saved memory records an earlier slip).
2. **Implement** host-side only. The 5-tool schema (`list_files, read_file, search_files,
   edit_file, write_file` + `run_git`/`run_command` + `delegate`) is FROZEN — do not touch it.
   Keep pure/testable logic in importable modules; keep TUI wiring thin (the codebase pattern:
   `difffmt`, `toolline`, `cmdguard`, `evalstats`, `driftreplay` are pure and unit-tested; the
   TUI is not pilot-tested in-suite).
3. **Adversarial review:** dispatch the `codeObserver` subagent on the staged diff (write the
   diff to a scratch file, pass its path). It has repeatedly caught real bugs unit tests and a
   single-path pilot miss — take its CRITICAL/HIGH findings seriously; fix with regression tests.
4. **Unit tests green**, then **real-project validation** (see harness below).
5. **Commit as Alexander** via `git commit -F <file>` (backticks in `-m` get shell-executed).
   NO `Co-Authored-By: Claude` trailer; neutral metadata (no competitor/comparison names in
   messages, branches, filenames). Verify: `git log -1 --format='%an <%ae>%n%(trailers)'`.
6. **Push branch → fast-forward merge to `main` → push `main`.** Verify `local == origin`.

## Real-project validation harness (the installed `2b` is a published build, not dev code)

- **Dev interpreter:** `/Users/do519-lap/.local/share/uv/tools/2b-agent/bin/python3`
  with `PYTHONPATH=/Users/do519-lap/repo_apps/2B/src`. Invoke the CLI as
  `"$VENV" -c "import sys; from two_b.cli import main; sys.exit(main())" <args>`.
- **Local model:** `qwen3:8b` (fast). `qwen3.5:9b` was too slow.
- **Test project:** rsync a copy of `/Users/do519-lap/repo_apps/a2_core_package` into a scratch
  dir (a real Dart package: `lib/src/{config,tool,agent,engine}.dart`, `lib/a2_core.dart`).
- **Useful envs:** `TWOB_HISTORY_DB` (temp sqlite), `TWOB_CONTEXT_TOKENS` (shrink window to force
  compaction), `TWOB_TRACE` (JSONL trace), `TWOB_NO_HISTORY`, `TWOB_SAMPLING_SEED` (eval only),
  `TWOB_EVAL_CLI` (point the eval harness at a dev build).
- **TUI validation:** Textual `app.run_test()` pilot (headless) drives the real event loop —
  see how the P19/P11 pilot exercised `/tool`, `/history`, risk labels, double-Ctrl-C. Textual
  8.2.8: `Static` has no `.renderable`; use `w.render()` (returns a `Content`/`Text` with
  `.plain`) or a stashed `_search_text`.
- **macOS gotchas:** `sleep`/`timeout` unavailable in the harness shell; run long model calls in
  the background. Use `$CLAUDE_JOB_DIR/tmp` (or the session scratchpad) for temp files.

## Design invariants (from the 2b design-philosophy memory)

- Frozen schema; all complexity host-side; native Ollama `/api/chat`; stdlib-heavy; simplicity/
  YAGNI; optimized for small-local-model reliability.
- Model-aware tool exposure: local Ollama → `run_git` only; cloud → `run_command` + `delegate`.
- Improvements are almost all universal (benefit cloud too); only provider-specific bits are
  local-only (Ollama sampling/keep-alive/done_reason/`TWOB_SAMPLING_SEED`, the Ollama-port kill
  guard). Nothing shipped regresses cloud.
- Use `AskUserQuestion` when a design tension would reverse a documented deliberate choice.

## To resume

The phased roadmap is complete and P18 is closed — there is **no queued build work**. Confirm `main`
is at `940f6bc` and 544 tests pass. The only outstanding item is **Linux CI behavioral validation**
of the bwrap paths (needs a Linux box). Otherwise, take direction from the user; optional future
ideas live in the two survey docs. If a new phase is taken, follow the per-phase process above and
always run the `codeObserver` review on the staged diff — across the security theme it caught ~14
real issues (three critical: a subprocess hang, an outline injection-bypass, a bwrap backdoor gap)
that unit tests alone missed.
