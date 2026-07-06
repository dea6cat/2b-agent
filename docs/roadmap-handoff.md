# 2B — status & handoff

Single source of truth for where the project stands and how to work on it. This replaces the
earlier per-theme roadmap docs (capabilities / performance-reliability / sandbox / security-
hardening), which are folded into the "Shipped" and "Open follow-ups" sections below.

## Where things stand

- **Branch/commit:** `main` at `28ac8ba` (`local == origin`). Working tree clean.
- **Tests:** full suite **616 green** — `python -m unittest discover -s tests -p "test_*.py"`
  (5 Linux-gated bwrap behavioral tests skip on macOS). Verified on **Python 3.13 and 3.14.6**.
- **Distribution:** published on PyPI as `2b-agent`; installable via the `curl … | sh` installer,
  `uv tool`, `pipx`, plain `pip`, and a Homebrew formula (`packaging/homebrew/`).

## Shipped

All landed as single feature commits, each adversarially reviewed (`codeObserver`) and validated.
The 5-tool schema stayed frozen throughout; all complexity is host-side.

- **Core agent.** Frozen tool schema (`list_files, read_file, search_files, edit_file, write_file`
  + `run_git`/`run_command` + `delegate`); tolerant edit matching + post-edit diagnostics; semantic
  symbol resolution (LSP → MCP → regex floor); scrollback search; `2b trace replay` prompt-drift
  detection; searchable compaction archive; project-instructions injection (CLAUDE.md/AGENTS.md);
  eval rigor (`evalstats.py`: exact-match scorer, bootstrap CIs, McNemar, multi-seed, publish guard);
  prefix stability + keep-alive + token calibration + structured compaction.
- **Security.** Two-layer `run_command` safety: seatbelt write-confinement (macOS `sandbox-exec` /
  Linux `bwrap`, on by default, `TWOB_NO_SEATBELT` / `TWOB_SEATBELT=strict`) + `cmdguard` command-
  approval (network/escalation/secrets-path → confirm); `strict` also confines **reads** ($HOME
  secrets unreadable); subprocess env scrub (`tools._child_env`) + bounded child output; prompt-
  injection fencing (`untrusted.py`, `<untrusted_data>` markers + data-not-instructions rule);
  SSRF-guarded web fetch. Model-aware exposure: local Ollama → `run_git` only; cloud → `run_command`
  + `delegate`.
- **Onboarding & distribution.** `2b setup` (machine grade → live model discovery from ollama.com,
  tool-capable + RAM-fitting + popularity-ranked → cost-confirmed pre-test of the top-N → menu of
  proven passers → pull → self-test → persist default → PATH); offers to `ollama rm` pre-tested
  models you didn't keep (`--keep-tested`). `2b --test [<model>|auto]` re-grades installed models
  (auto removes failures, never the default, TTY-gated). `--doctor`, `--rm`, `--update`, `--setup`.
  `/fetch <url>` pulls readable web content into context (host-side, not a model tool). `2b setup`
  installs Ollama if missing / uses it if present.
- **Install-method awareness.** `update._install_kind()` classifies uv / pipx / brew / pip by file
  path; `--update`, `--rm`, and `2b setup`'s PATH fix each dispatch on it (correct upgrade/uninstall
  command per installer; correct scripts dir incl. pip `--user` scheme; no-op PATH fix for brew).
- **Python 3.14.** Dependency floors raised to 3.14-supported releases (`rich>=14.2`,
  `prompt_toolkit>=3.0.52`, `textual>=6.3`, `mcp>=1.28`); trove classifiers 3.11–3.14; the toolchain
  runs on 3.14.6.
- **Local-model tool-call reliability.** Host-side recovery of tool calls a model emits as
  text instead of the native `tool_calls` field: `tools.recover_toolcalls` promotes fenced
  ```json`` / whole-body JSON (via `loads_tolerant`) into real calls in `providers/ollama`'s
  `send`/`stream`, only when native calls are absent. Measured cause: `qwen2.5-coder:14b` emits
  100% of calls as text and did *nothing* in 2B before this — after, it executes calls and lands
  edits (0→47 calls on the 7-task real-project suite; qwen3:8b/qwen3.5:9b unchanged, 0 residual
  uncaught calls). Plus a bounded intent-stall nudge (`_stalled_without_acting` / `_STALL_RE`):
  a no-tool-call turn narrating intent + an investigative verb, gated on zero actions taken and
  fired once, so a model that says "let me first explore…" and stops is nudged to act (ordinary
  sign-offs like "let me know if…" are excluded). **Accepted trade-off:** a fenced ```json`` tool-call
  *example* inside a no-native-call answer will execute — irreducible, since the target model
  prepends prose to its real calls; gated to zero-native-call turns and bounded by the P12 seatbelt.
- **Homebrew.** `packaging/homebrew/Formula/twob-agent.rb` — a `Language::Python::Virtualenv`
  formula (named `twob-agent`, not `2b-agent`, because a leading-digit formula name yields an invalid
  Ruby class; it installs the `2b` command). Verified locally: `brew style`/`audit` clean, source
  build succeeds, `2b --version` runs on brew's Python 3.14. See `packaging/homebrew/README.md`.

## Open follow-ups

No queued build work; take direction from the user. Known items, most valuable first:

0. **Finish Spanish stall detection.** `_STALL_RE` includes Spanish intent openers (`voy a`,
   `déjame`) but an English-only investigative-verb list, so a real Spanish stall
   ("voy a explorar…") never matches. Add Spanish verbs (anchored to avoid `ver`→"very"-class
   false positives) or drop the dead openers. Part of the broader "Spanish steering" optional item
   (extend `_INTENT_RE`/`_DANGLING_RE` too). Deferred also: exec-tool arg unwrap for text-emitted
   `run_git`/`run_command` (currently yields a recoverable error, not a wrong action); stream-buffering
   so a suspected tool-call blob isn't shown before recovery strips it.

1. **Make `mcp` an optional extra.** It's lazily imported, but as a hard dependency it drags in
   `cryptography` / `pydantic-core` / `rpds-py` — a heavy native tree. Optional-izing it collapses
   the Homebrew formula to three pure-Python resources (no Rust/LLVM toolchain, no ~4.5-min build)
   and lightens `pip install` too. Highest-value cleanup; aligns with the minimal-deps thesis.
2. **Publish the Homebrew tap.** The live tap is a separate GitHub repo `dea6cat/homebrew-2b`
   (not yet pushed). Steps in `packaging/homebrew/README.md`. Formula already verified.
3. **`brew` self-detection.** The `brew` case in `_install_kind` postdates PyPI `1.1.1`, so a
   `1.1.1`-pinned formula self-classifies as `pip` at runtime (install + `2b` work regardless). Cut a
   new release from `main` and bump the formula to make `--update`/`--rm` say `brew`.
4. **Linux CI behavioral validation** of the bwrap write/read-confinement paths (the 5 gated tests +
   a userns smoke check). Needs a Linux environment; everything else is validated on macOS.
5. **S1 read-side workspace jail** — deliberately NOT done: it reverses 2B's documented
   "point-anywhere" file access (S7 already confirms *secrets* reads). Needs an explicit
   `AskUserQuestion` before starting. Lower-priority optional ideas (path-scoped grants, TUI polish,
   per-model catalog extras) remain optional.

## The per-phase process (follow exactly — the user relies on it)

1. **Branch off `main` first.** `git fetch origin && git checkout -b feat/<name> origin/main`.
   NEVER commit phase work directly to `main`.
2. **Implement** host-side only. The 5-tool schema is FROZEN — do not touch it. Keep pure/testable
   logic in importable modules; keep TUI wiring thin (`difffmt`, `toolline`, `cmdguard`, `evalstats`,
   `driftreplay` are pure and unit-tested).
3. **Adversarial review:** dispatch the `codeObserver` subagent on the staged diff. Take its
   CRITICAL/HIGH findings seriously; fix with regression tests. (Across the security theme it caught
   ~14 real issues — three critical: a subprocess hang, an outline injection-bypass, a bwrap backdoor
   gap — that unit tests alone missed.)
4. **Unit tests green**, then **real-project validation** (harness below).
5. **Commit as Alexander** via `git commit -F <file>` (backticks in `-m` get shell-executed). NO
   `Co-Authored-By: Claude` trailer; neutral metadata (no competitor names in messages/branches/
   filenames). Verify: `git log -1 --format='%an <%ae>%n%(trailers)'`.
6. **Push branch → fast-forward merge to `main` → push `main`.** Verify `local == origin`.

## Real-project validation harness (the installed `2b` may be a published build, not dev code)

- **Dev interpreter:** `/Users/do519-lap/.local/share/uv/tools/2b-agent/bin/python3` with
  `PYTHONPATH=/Users/do519-lap/repo_apps/2B/src`; invoke as
  `"$VENV" -c "import sys; from two_b.cli import main; sys.exit(main())" <args>`.
- **Local model:** `qwen3:8b` (fast). **Test project:** rsync a copy of
  `/Users/do519-lap/repo_apps/a2_core_package` into a scratch dir.
- **Useful envs:** `TWOB_HISTORY_DB`, `TWOB_CONTEXT_TOKENS`, `TWOB_TRACE`, `TWOB_NO_HISTORY`,
  `TWOB_SAMPLING_SEED` (eval), `TWOB_EVAL_CLI`, `TWOB_NO_MODEL_FETCH` (force bundled model list).
- **TUI validation:** Textual `app.run_test()` pilot (headless). Textual 8.2.8: `Static` has no
  `.renderable`; use `w.render()`.
- **macOS gotchas:** `sleep`/`timeout` unavailable in the harness shell; run long model calls in the
  background; use the session scratchpad for temp files.

## Design invariants

- Frozen schema; all complexity host-side; native Ollama `/api/chat`; stdlib-heavy; simplicity/YAGNI;
  optimized for small-local-model reliability.
- Improvements are almost all universal (benefit cloud too); only provider-specific bits are
  local-only. Nothing shipped regresses cloud.
- Use `AskUserQuestion` when a design tension would reverse a documented deliberate choice.
