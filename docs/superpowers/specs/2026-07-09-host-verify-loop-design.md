# Host-run verify-and-fix loop

**Date:** 2026-07-09
**Status:** Approved (design), pending spec review → plan
**Branch:** `feat/host-verify-loop`

## Problem / goal

2B's whole purpose is *real* development capability with local models. Today a local model
edits files but has no way to know its edits compile/pass: it's restricted to `run_git` (not
`run_command`, `toolspec.py:specs_for`), so it can't run the project's build/tests. 2B already
*discovers* the project's checks (`verify.discover_checks`) but **runs nothing** — it only feeds
a *reminder nudge*, and that nudge is **cloud-only** (`orchestrator.py:1588`, gated `not is_local`).

**Goal:** after the model finalizes with edits that landed, **2B itself runs the project's
checks**, and on failure feeds the errors back for a **bounded fix loop**. Because the *host*
runs them, local models get real toolchain grounding **without** being granted `run_command`.
This is the deterministic counterpart to "declare done, then actually verify," and the natural
successor to the false-success guard (which catches edits that never applied; this catches edits
that applied but don't pass).

## Decisions (from brainstorming)

- **Depth:** run **all** discovered checks including test suites **by default** (strongest
  correctness signal). Mitigated by the safety valves below (a fast-only opt-down, timeouts,
  cancel, bounded rounds) so it can't wedge a slow local session.
- **Scope:** applies to **both local and cloud**. Host execution *replaces* the cloud-only
  verify-nudge (running the checks is more reliable than asking the model to).
- **Language-agnostic by construction** (see below) — Flutter/Dart is only an example.

## Language-agnosticism

The loop is generic; *all* language knowledge stays in two existing, isolated detectors:
- `verify.discover_checks(root)` — manifest → check commands. Covers **Node/JS/TS**, **Python**,
  **Dart/Flutter**, and (this change) **Go**, **Swift**, **Kotlin** — full rules in §0 below.
- `diagnostics.check(path)` — file-extension → per-file static check (already Python/Dart);
  unchanged, keeps running after each edit for fast in-flight feedback.

Consequences:
- **Unknown stacks degrade to a no-op** — if `discover_checks` returns `[]` and there's no
  user override, the loop does nothing. Never worse than today.
- **Escape hatch for the long tail** (monorepos, Bazel, bespoke scripts, or to override
  detection): `TWOB_VERIFY_CMD` — a user-declared command list (newline- or `;;`-separated).
  When set, it *replaces* discovery. Covers any stack without 2B knowing the ecosystem.
- **Broadening auto-detection is additive in `discover_checks`** — never touches the loop.
  This change adds JavaScript/TypeScript, Go, Swift, and Kotlin (see §0 below). Still-future
  candidates (covered now by `TWOB_VERIFY_CMD`): Rust (`cargo check`/`cargo test`), `make test`,
  Maven (`mvn`). `diagnostics.check(path)` per-file handlers for `.ts`/`.go`/`.swift`/`.kt` are
  a **separate, optional** follow-up (fast in-flight per-file feedback) — out of scope here; the
  project-level checks below are what feed the verify loop.

## §0. Language detectors (`verify.discover_checks`) — v1 scope

Each detector is best-effort, gated on a manifest/config file, order-stable, deduped by the
existing `add()`. The runner skips any command whose binary isn't installed, so listing a
command is always safe. Prefer the project's own declared scripts; add direct tool invocations
only to fill gaps.

| Stack | Trigger file(s) | Fast/static checks | Test checks |
|---|---|---|---|
| **Node / JS / TS** (extend existing) | `package.json` | `npm run <lint\|typecheck\|check>` (existing scripts); **+** `tsc --noEmit` if `tsconfig.json` exists **and** no `typecheck`/`check` script; **+** `eslint .` if an eslint config (`.eslintrc*`, `eslint.config.*`) exists **and** no `lint` script | `npm run test` (existing) |
| **Python** (existing) | `pyproject.toml`/`setup.py` | `ruff check .` (if ruff configured) | `pytest` (if `tests/`) |
| **Dart/Flutter** (existing) | `pubspec.yaml` | `dart analyze` | `dart test`/`flutter test` |
| **Go** (new) | `go.mod` | `go build ./...` | `go test ./...` |
| **Swift** (new) | `Package.swift` (SwiftPM; Xcode-project `xcodebuild` is out of scope) | `swift build` | `swift test` |
| **Kotlin** (new) | `build.gradle.kts` or `build.gradle` | `<gradle> check` | `<gradle> test`, where `<gradle>` is `./gradlew` if the wrapper exists, else `gradle` |

Notes:
- JS/TS: the existing `package.json`-script detection already covers projects that expose
  `test`/`lint`/`typecheck` scripts; the new `tsc`/`eslint` fallbacks only fire when the
  matching script is absent, so we never run a linter twice.
- Kotlin uses the Gradle wrapper (`./gradlew`) when present (no global Gradle needed); this also
  covers Gradle-based Java, though Java isn't a target here.
- All new commands are classified correctly by `classify()` (below) — `build`/`check` → fast,
  `test` → tests.

## Design

### 1. Check classification (`verify.py`)

Add a helper to split discovered commands into **fast/static** vs **tests**, by a
language-neutral name heuristic:
- fast (static): command contains `analyze`, `lint`, `typecheck`, `check`, `ruff`, `vet`,
  `tsc`, `mypy`, or `build` (covers `go build`, `swift build`, `<gradle> check`, `tsc`, `eslint`).
- tests: command contains `test` or `spec`.
  (Order matters: a command like `swift test` is tests, `swift build` is fast; classify on the
  tests keyword first so a `build` substring can't misclassify a test command.)
- anything unmatched is treated as fast (conservative — run it, it's usually quick).

Used so the fast-only opt-down (`TWOB_VERIFY_FAST`) works generically even though the default
runs everything.

New: `discover_or_override(root) -> list[str]` — returns `TWOB_VERIFY_CMD` commands if set,
else `discover_checks(root)`. `classify(cmds) -> (fast, tests)`.

### 2. Runner (`verify.py`)

`run_checks(cmds, *, cancel, per_cmd_timeout) -> list[CheckResult]` where
`CheckResult = (cmd: str, ok: bool, output: str)`.
- Executes each command host-side via the existing cancellable subprocess runner
  (`tools._run_cancellable`, shell=True, `env=tools._child_env()` for secret-scrubbing),
  honoring `task.cancel_flag` so **ESC aborts a running verify** (ties into the global-abort
  work) and a per-command timeout.
- Non-zero exit → `ok=False`; output (stdout+stderr, truncated) captured for feedback.
- A missing command binary (`FileNotFoundError`) → **skip that check** (not a failure) — never
  fail a task because the toolchain isn't installed.
- Does **not** route through the model-facing `cmdguard` confirmation — these are host-initiated,
  bounded, discovered/config'd commands, not model input.

### 3. The loop (`orchestrator.py`, finalize path)

Replaces the current cloud-only verify-nudge block. When the model finalizes (final answer, no
tool calls) AND `task.edit_history` is non-empty (edits landed) AND verification is enabled AND
there are checks:

```
rounds = 0
on each finalize with landed edits:
  cmds = fast-only if TWOB_VERIFY_FAST else all discovered/override cmds
  status_line = "Verifying"; emit progress (per command)
  results = verify.run_checks(cmds, cancel=task.cancel_flag, per_cmd_timeout=…)
  failures = [r for r in results if not r.ok]
  if not failures:                      # all green
      (optional) append a brief "✓ checks passed (<cmds>)" to the final answer
      finish DONE
  elif rounds < MAX_VERIFY_ROUNDS:      # feed back, let it fix
      rounds += 1
      conv.append(model msg)
      conv.append(Message.user("Your edits did not pass the project checks. `<cmd>` failed:\n"
                               "<truncated output>\nFix the code so it passes, then finish."))
      continue
  else:                                 # exhausted — finish, but HONESTLY
      surface "checks still failing after N attempts:\n<failures>" in the final output
      finish DONE
```

- `MAX_VERIFY_ROUNDS = 2` (then stop — never loop forever; report the true state).
- Fires for local and cloud alike (no `is_local` gate).
- Read-only / no-edit tasks never trigger it (`edit_history` empty).
- Distinct from the false-success guard: that fires when `edit_history` is *empty* despite edit
  attempts; this fires when `edit_history` is *non-empty* but checks fail. Complementary.

### 4. Progress / UI

Emit a status ("Verifying — running `<cmd>`…") and a log line per check, so there's no silent
gap while (possibly slow) tests run — same principle as the `--test` progress work. ESC stops it
(cancel-aware runner).

### 5. Safety valves (all documented in PRIVACY/README)

- `TWOB_NO_VERIFY=1` — skip the whole thing (already the discovery toggle).
- `TWOB_VERIFY_FAST=1` — static checks only; skip test suites (speed opt-down for slow boxes).
- `TWOB_VERIFY_CMD="…"` — user-declared checks (custom stacks / override).
- Per-command timeout + `task.cancel_flag` (ESC) so a hung suite can't wedge the session.
- Bounded fix rounds; infra errors (missing binary) skip, never fail.

## Testing

- `verify.classify` — fast vs tests split across every stack's command strings, incl. the
  edge cases: `swift build`/`go build`/`<gradle> check` → fast, `swift test`/`go test ./...`/
  `<gradle> test` → tests (tests keyword wins over a `build` substring), unmatched → fast.
- `verify.discover_checks` per new stack, using tmp project dirs with only the trigger file:
  - `go.mod` → `go build ./...`, `go test ./...`
  - `Package.swift` → `swift build`, `swift test`
  - `build.gradle.kts` (+ a `gradlew` file) → `./gradlew check`, `./gradlew test`; without the
    wrapper → `gradle check`, `gradle test`
  - `tsconfig.json` with no `typecheck`/`check` script → includes `tsc --noEmit`; **with** a
    `typecheck` script → does **not** duplicate (`npm run typecheck` only)
  - eslint config with no `lint` script → includes `eslint .`; with a `lint` script → no dupe
  - unknown project (no manifest) → `[]`
- `verify.discover_or_override` — returns `TWOB_VERIFY_CMD` when set (parsed), else discovery.
- `verify.run_checks` — a passing command (`sh -c "exit 0"`) → ok; a failing one
  (`sh -c "echo boom; exit 1"`) → not ok, output captured; a missing binary → skipped, not
  failed; honors a pre-set cancel Event (returns promptly).
- Loop trigger predicate (edits landed + checks exist + enabled) — small pure helper, unit test.
- Full suite green; the cloud verify-nudge test (if any) updated to the new host-run behavior.

## Out of scope (YAGNI)

- Auto-detectors beyond v1's set (Node/JS/TS, Python, Dart, Go, Swift, Kotlin) — Rust
  (`cargo`), `make`, Maven (`mvn`), and Xcode-project `xcodebuild` are additive later; the
  `TWOB_VERIFY_CMD` escape hatch covers them now.
- Per-file `diagnostics.check` handlers for `.ts`/`.go`/`.swift`/`.kt` (in-flight per-edit
  feedback) — a separate, optional follow-up; v1 extends the project-level checks only.
- Structured parsing of check output — the model reads truncated raw output (as it already does
  for `dart analyze`).
- Confirmation prompts for host-run checks — they're bounded/known; `TWOB_NO_VERIFY` is the
  off switch.
