# Roadmap execution handoff

Working state for resuming the `docs/performance-reliability-roadmap.md` execution in a
fresh session. Last updated after the P19+P10+P11 cluster merged.

## Where things stand

- **Branch/commit:** `main` at `527777c` (`local == origin`). Working tree clean.
- **Tests:** full suite **471 green** — `python -m unittest discover -s tests -p "test_*.py"`.
- **Shipped on `main` (22 phases):** P1, P2, P3, P4, P5, P6, P7, P8, P9, P10, P11, P13,
  P14, P15, P16, P17, P20, P22, P23, P25, P27. Each landed as one squash-free feature
  commit (see `git log --oneline`), reviewed and validated (below).

### What each recent cluster added (one line each)
- **P19+P10+P11** (`527777c`): `/tool` direct tool invocation, `/history search` scrollback
  nav, risk-class label on confirmations; `2b trace replay <id>` prompt-drift detection
  (salted prefix hash per session + `driftreplay.py`); TUI Esc-snap-to-bottom + double-Ctrl-C.
- **P8+P9** (`d791ee0`): project-instructions injection (CLAUDE.md/AGENTS.md → system prompt);
  eval rigor — `evalstats.py` (exact-match scorer, seeded bootstrap CIs, McNemar, multi-seed
  variance, dirty-tree publish guard), harness multi-seed + trace copy-out + `TWOB_EVAL_CLI`.
- **P17** (`89f2b1c`): searchable compaction archive behind lossy compaction + dangling-ref recall.
- **P5/P6/P22/P27** (`19cc21d`): prefix stability + keep-alive, token calibration, structured
  compaction summary.

## Remaining work (consolidated priority order, item #12 — the last)

Both are optional/conditional per the roadmap; **get an explicit go-ahead before starting** —
neither is on the critical path.

- **P18 — Scoped-prompt + bounded state** (roadmap line ~212). For `delegate`/sub-runner work:
  assemble a fresh minimal prompt with a compact ring-buffered state object regenerated each
  call instead of carrying full history. Files: `subagents.py` (delegate prompt assembly), a
  small `state.py` dataclass; new `tests/test_scoped_state.py`. **Note:** only worth doing if
  2B's subtask execution actually grows — otherwise defer. Check whether `subagents.py`/delegate
  is exercised enough to justify it before proposing.
- **P12 — macOS `sandbox-exec` seatbelt** (roadmap line ~122). Optionally wrap `run_command` in
  a `sandbox-exec` profile confining writes to the workspace root. Opt-in; degrade gracefully
  where unavailable. Files: new `seatbelt.py`, wired in `tools.do_run_command`; tests gated on
  Darwin. A genuine safety lead if pursued; off the critical path.

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

Read `docs/performance-reliability-roadmap.md` (full specs) + this file, confirm `main` is at
`527777c` and 471 tests pass, then ask the user which of P18 / P12 (if any) to take — do not
start either without an explicit go-ahead.
