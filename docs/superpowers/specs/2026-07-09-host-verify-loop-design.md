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
| **Kotlin** (new) | `build.gradle.kts` or `build.gradle` | — (none in v1, see note) | `<gradle> check` — `<gradle>` = `./gradlew` if the wrapper exists, else `gradle` |

The Fast/Tests columns are authoritative: **`discover_checks` returns `(cmd, kind)` pairs**, with
`kind ∈ {"fast","tests"}` assigned by the detector that emitted the command (it knows the tool's
semantics). The fuzzy `classify()` heuristic (§1) is used **only** for user-supplied
`TWOB_VERIFY_CMD` commands, where 2B can't know the tier.

Notes:
- JS/TS: the existing `package.json`-script detection already covers projects that expose
  `test`/`lint`/`typecheck` scripts; the new `tsc`/`eslint` fallbacks only fire when the
  matching script is absent, so we never run a linter twice.
- **Kotlin/Gradle: `check` is test-inclusive by design** — in Gradle's lifecycle `check`
  dependsOn `test`, so `<gradle> check` runs the full suite. It is therefore emitted as a
  **single command classified as `tests`** (not fast), which (a) means `TWOB_VERIFY_FAST`
  correctly skips it and (b) avoids running the suite twice (no separate `<gradle> test` entry).
  v1 ships **no** fast/static Kotlin check: a portable compile-only Gradle task doesn't exist
  (`compileKotlin` vs Android's `compileDebugKotlin` vs base `testClasses` all vary, and naming a
  missing task fails the check), so a fast Kotlin check is left to `TWOB_VERIFY_CMD`
  (e.g. `./gradlew ktlintCheck` or `detekt` for projects that have them). Uses the wrapper
  (`./gradlew`) when present so no global Gradle is needed.

## Design

### 1. Tiers (`verify.py`)

Every check carries a `kind ∈ {"fast","tests"}` so `TWOB_VERIFY_FAST` can drop the test tier
generically. **Detected checks get their kind from the detector** (§0 columns) — authoritative,
because the detector knows e.g. that Gradle `check` runs tests but npm `check` doesn't. A
substring heuristic can't make that distinction, which is exactly why the tier is assigned at
detection, not inferred afterward.

`classify(cmd) -> "fast" | "tests"` is a **fallback used only for user `TWOB_VERIFY_CMD`
commands** (unknown tier): `tests` if the command contains `test` or `spec` (checked first),
else `fast`. Tests-keyword-first so `swift test` → tests while `swift build` → fast; unmatched →
fast (conservative). It is *not* applied to detected checks.

APIs:
- `discover_checks(root) -> list[(cmd, kind)]` — the detectors in §0, each pair tier-tagged.
- `discover_or_override(root) -> list[(cmd, kind)]` — if `TWOB_VERIFY_CMD` is set, split it into
  commands and tag each via `classify()`; else `discover_checks(root)`.

### 2. Runner (`verify.py`)

`run_checks(checks, *, cancel, per_cmd_timeout, on_start=None) -> list[CheckResult]` where
`checks` is the `(cmd, kind)` list and
`CheckResult = (cmd: str, status: "pass"|"fail"|"skipped"|"cancelled", output: str)`.
- `on_start(cmd)` is invoked immediately before each command runs, so the loop/UI can emit a
  live "running `<cmd>`…" line (resolves the blocking-return-vs-progress gap — the caller drives
  progress per command without waiting for the whole batch).
- Executes each command host-side via the existing cancellable subprocess runner
  (`tools._run_cancellable`, shell=True, `env=tools._child_env()` for secret-scrubbing),
  honoring `cancel` (`task.cancel_flag`) so **ESC aborts a running verify** (ties into the
  global-abort work) and a per-command timeout.
- **Missing binary → `skipped`, not `fail`.** Under `shell=True` a missing binary returns shell
  exit **127**, not a `FileNotFoundError` — so detect it *before* running: `shutil.which(first
  shlex token)`; if `None`, record `skipped` and move on. (Verified: `shell=True` raises no
  exception for a missing command; `shutil.which` resolves both PATH names and `./gradlew`.)
- **`cancelled` is distinct from `fail`.** `_run_cancellable` returns a `"cancelled"` status on
  abort/timeout-kill; map that to `CheckResult.status == "cancelled"` so the loop can stop the
  task cleanly instead of treating an ESC as a rejected edit (see §3).
- Otherwise: exit 0 → `pass`; non-zero → `fail`, with combined stdout+stderr captured.
- **Output truncation:** cap each command's captured output and keep **head + tail** (reusing
  `do_run_command`'s existing head-⅔/tail-⅓ elision), not tail-only — compiler/type errors
  (`tsc`, `go build`, `dart analyze`) lead at the top while test summaries land at the bottom, so
  keeping both ends preserves the actionable lines for either kind of check.
- Does **not** route through the model-facing `cmdguard` confirmation — these are host-initiated,
  bounded, discovered/config'd commands, not model input.

### 3. The loop (`orchestrator.py`, finalize path)

Replaces the current cloud-only verify-nudge block. When the model finalizes (final answer, no
tool calls) AND `task.edit_history` is non-empty (edits landed) AND verification is enabled AND
there are checks:

```
rounds = 0
on each finalize with landed edits:
  checks = [(c,k) for (c,k) in discover_or_override(cwd)
            if not (TWOB_VERIFY_FAST and k == "tests")]     # fast-only drops the test tier
  status_line = "Verifying"
  results = verify.run_checks(checks, cancel=task.cancel_flag, per_cmd_timeout=…,
                              on_start=lambda c: emit("running " + c + "…"))
  if any(r.status == "cancelled" for r in results) or task.cancel_flag.is_set():
      _finish_stopped(task, on_event); return          # ESC — NOT a failure, no round consumed
  failures = [r for r in results if r.status == "fail"]  # "skipped" (missing tool) is not a failure
  if not failures:                        # all green (or only skips)
      (optional) append a brief "✓ checks passed (<ran cmds>)" to the final answer
      finish DONE
  elif rounds < MAX_VERIFY_ROUNDS:        # feed back ALL failures, let it fix
      rounds += 1
      conv.append(model msg)
      body = "\n\n".join(f.cmd + " failed:\n" + untrusted.wrap(f.output, "check:" + f.cmd)
                         for f in failures)
      conv.append(Message.user("Your edits did not pass the project checks. Fix the code so "
                               "these pass, then finish:\n\n" + body))
      continue
  else:                                   # exhausted — finish, but HONESTLY
      surface "checks still failing after N attempts:" + all failing cmds (fenced) in the output
      finish DONE
```

Three review-driven specifics baked in above:
- **All failures in a round are reported together** (`failures`, joined) — if `tsc` *and*
  `npm test` both fail, the model sees both and fixes them in one round instead of wasting a
  round blind to the second.
- **Check output is fenced with `untrusted.wrap(output, "check:<cmd>")`** before it enters the
  conversation as a user-role message — build/test output is environment-derived text and could
  contain injection-shaped content; this is the same mechanism tool results already use.
- **A cancelled check ends the task via `_finish_stopped`** and neither consumes a fix round nor
  is fed back as a rejection — an ESC during verify is a stop, not a failed edit.

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

- `verify.classify` (user-command fallback only) — `swift test`/`go test ./...`/`npm test` →
  tests; `swift build`/`go build`/`tsc --noEmit`/`eslint .`/`npm run lint` → fast (tests keyword
  wins over any `build` substring); unmatched → fast.
- `verify.discover_checks` returns `(cmd, kind)` pairs; per new stack, using tmp project dirs
  with only the trigger file:
  - `go.mod` → `("go build ./...","fast")`, `("go test ./...","tests")`
  - `Package.swift` → `("swift build","fast")`, `("swift test","tests")`
  - `build.gradle.kts` **with** a `gradlew` file → `("./gradlew check","tests")` **only** (single
    test-inclusive command; **no** separate `test` entry, **not** classified fast); **without**
    the wrapper → `("gradle check","tests")`
  - `tsconfig.json`, no `typecheck`/`check` script → includes `("tsc --noEmit","fast")`; **with**
    a `typecheck` script → no dupe (`npm run typecheck` only)
  - eslint config, no `lint` script → includes `("eslint .","fast")`; with a `lint` script → no dupe
  - unknown project (no manifest) → `[]`
- `verify.discover_or_override` — `TWOB_VERIFY_CMD` set → its parsed commands, each tagged via
  `classify()`; else `discover_checks`.
- `verify.run_checks` — `sh -c "exit 0"` → `pass`; `sh -c "echo boom; exit 1"` → `fail`, output
  captured; a **missing binary** (`no-such-cmd …`) → `skipped` (via `shutil.which`, *not* a
  `fail` at exit 127); a pre-set cancel Event → returns promptly with `cancelled`; `on_start`
  fires once per command.
- Loop behavior (drive `run_task` with a fake provider, matching the nudge-test precedent, or
  test the extracted decision helper): all-green → DONE; a `fail` → one corrective turn carrying
  **every** failing cmd's fenced output; `cancelled`/`cancel_flag` set → `_finish_stopped`, no
  round consumed, no feedback; `TWOB_VERIFY_FAST` drops `kind=="tests"` (so a Gradle-only project
  runs nothing under fast); `MAX_VERIFY_ROUNDS` cap stops after N and reports honestly.
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
