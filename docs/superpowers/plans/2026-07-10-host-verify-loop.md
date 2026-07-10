# Host-run Verify-and-Fix Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** After the model finalizes with edits that landed, 2B itself runs the project's checks (language-agnostic) and, on failure, feeds the errors back for a bounded fix loop — giving local models real toolchain grounding without granting `run_command`.

**Architecture:** All language knowledge stays in `verify.discover_checks` (manifest → `(cmd, kind)` pairs) and a cancellable `verify.run_checks` runner; the loop in `orchestrator.run_task`'s finalize path is generic. Replaces the existing cloud-only verify-*nudge* with host execution for local + cloud.

**Tech Stack:** Python 3, stdlib only, `unittest`. Reuses `tools._run_cancellable` (subprocess + timeout + cancel) and `untrusted.wrap`.

**Spec:** `docs/superpowers/specs/2026-07-09-host-verify-loop-design.md` (read it — it has the full rationale and the resolved review findings).

## Global Constraints

- **Stdlib only**; `verify.py` must **never raise** (best-effort, wrapped in try/except like the existing code).
- Tests: `unittest`, run as `python -m unittest tests.test_<name>`, `sys.path.insert(0, .../src)` header; mirror existing test style. Run via `.venv/bin/python`.
- **`discover_checks` return type changes** from `list[str]` to `list[tuple[str, str]]` — the one existing caller (`orchestrator.py:1493`, feeding the verify-nudge) must be kept working in Task 1 and is fully replaced in Task 3.
- Tiers: `kind ∈ {"fast","tests"}` assigned **by the detector**; `classify()` is a fallback for `TWOB_VERIFY_CMD` only. Tests-keyword-first in `classify` (`swift test`→tests, `swift build`→fast).
- Kotlin `<gradle> check` is **test-inclusive → kind `"tests"`**, single command (no separate `test`), so `TWOB_VERIFY_FAST` skips it and it never double-runs.
- Missing binary → `skipped`, **not** `fail` (detect with `shutil.which` on the first token; `shell=True` returns exit 127, never `FileNotFoundError`).
- Cancelled check → `_finish_stopped`, no round consumed, not fed back.
- Feed back **all** failures in a round; **fence** each with `untrusted.wrap(output, "check:<cmd>")`.
- Head+tail output truncation.
- Branch note: this branch (`feat/host-verify-loop`) is stacked on `feat/local-model-reliability-guards` (unmerged) — the false-success guard is already present in the finalize block; the verify loop sits beside it.
- Commit after each task; commit as the repo user, no `Co-Authored-By`, neutral messages.

---

### Task 1: `verify.py` — tiered detection + new languages + override

**Files:**
- Modify: `src/two_b/verify.py` (`discover_checks` ~74-105; add helpers)
- Modify: `src/two_b/orchestrator.py:1493` and `:1593` (keep the existing nudge working against the new return type — minimal shim, fully replaced in Task 3)
- Test: `tests/test_verify_checks.py` (create)

**Interfaces:**
- Produces: `classify(cmd:str)->str`; `discover_checks(root=".")->list[tuple[str,str]]`; `discover_or_override(root=".")->list[tuple[str,str]]`; module constants `FAST="fast"`, `TESTS="tests"`.

- [ ] **Step 1: Write the failing test** — `tests/test_verify_checks.py`:

```python
"""verify.discover_checks tiers + new-language detection + TWOB_VERIFY_CMD override.
Run: `python -m unittest tests.test_verify_checks` from the repo root.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import verify  # noqa: E402


class Detect(unittest.TestCase):
    def _proj(self, files: dict) -> str:
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        for name, content in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(name) else None
            with open(p, "w") as f:
                f.write(content)
        return d

    def test_go(self):
        d = self._proj({"go.mod": "module x\n"})
        self.assertEqual(verify.discover_checks(d),
                         [("go build ./...", "fast"), ("go test ./...", "tests")])

    def test_swift(self):
        d = self._proj({"Package.swift": "// swift-tools-version:5.9\n"})
        self.assertEqual(verify.discover_checks(d),
                         [("swift build", "fast"), ("swift test", "tests")])

    def test_kotlin_gradle_check_is_single_test_inclusive(self):
        d = self._proj({"build.gradle.kts": "", "gradlew": "#!/bin/sh\n"})
        checks = verify.discover_checks(d)
        self.assertEqual(checks, [("./gradlew check", "tests")])  # single, test-inclusive, no fast, no separate test

    def test_kotlin_without_wrapper_uses_gradle(self):
        d = self._proj({"build.gradle": ""})
        self.assertEqual(verify.discover_checks(d), [("gradle check", "tests")])

    def test_ts_tsc_fallback_only_without_script(self):
        d = self._proj({"package.json": json.dumps({"scripts": {}}), "tsconfig.json": "{}"})
        self.assertIn(("tsc --noEmit", "fast"), verify.discover_checks(d))

    def test_ts_no_dupe_when_typecheck_script_present(self):
        d = self._proj({"package.json": json.dumps({"scripts": {"typecheck": "tsc"}}), "tsconfig.json": "{}"})
        checks = verify.discover_checks(d)
        self.assertIn(("npm run typecheck", "fast"), checks)
        self.assertNotIn(("tsc --noEmit", "fast"), checks)

    def test_eslint_fallback_only_without_lint_script(self):
        d = self._proj({"package.json": json.dumps({"scripts": {}}), ".eslintrc.json": "{}"})
        self.assertIn(("eslint .", "fast"), verify.discover_checks(d))

    def test_npm_test_is_tests_kind(self):
        d = self._proj({"package.json": json.dumps({"scripts": {"test": "jest"}})})
        self.assertIn(("npm run test", "tests"), verify.discover_checks(d))

    def test_unknown_project_empty(self):
        self.assertEqual(verify.discover_checks(self._proj({"README.md": "x"})), [])

    def test_classify_fallback(self):
        self.assertEqual(verify.classify("swift test"), "tests")
        self.assertEqual(verify.classify("go test ./..."), "tests")
        self.assertEqual(verify.classify("swift build"), "fast")
        self.assertEqual(verify.classify("eslint ."), "fast")
        self.assertEqual(verify.classify("make widget"), "fast")  # unmatched -> fast

    def test_override_env(self):
        os.environ["TWOB_VERIFY_CMD"] = "make lint;;make test"
        self.addCleanup(os.environ.pop, "TWOB_VERIFY_CMD", None)
        self.assertEqual(verify.discover_or_override(self._proj({})),
                         [("make lint", "fast"), ("make test", "tests")])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — expect FAIL** (`classify`/`discover_or_override` missing, `discover_checks` returns strings).

Run: `.venv/bin/python -m unittest tests.test_verify_checks -v`
Expected: FAIL (AttributeError / tuple mismatch).

- [ ] **Step 3: Rewrite `discover_checks` + add helpers** in `src/two_b/verify.py`. Add `import glob` and `import shutil` (shutil used in Task 2) near the top imports. Replace the whole `discover_checks` function (lines ~74-105) with:

```python
FAST, TESTS = "fast", "tests"


def classify(cmd: str) -> str:
    """Best-effort tier for a user-supplied TWOB_VERIFY_CMD command (detected checks carry
    their own tier from discover_checks). Tests keyword wins so `swift test` -> tests while
    `swift build` -> fast; anything unmatched -> fast (conservative — usually quick)."""
    low = cmd.lower()
    return TESTS if ("test" in low or "spec" in low) else FAST


def _has_eslint_config(root: str) -> bool:
    return bool(glob.glob(os.path.join(root, ".eslintrc*"))
                or glob.glob(os.path.join(root, "eslint.config.*")))


def discover_checks(root: str = ".") -> list[tuple[str, str]]:
    """The project's real check commands as (command, kind) pairs — kind in {'fast','tests'},
    assigned by the detector (it knows the tool's semantics). Read from manifests; deduped,
    order-stable; fast checks before tests within a stack. Never raises."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(cmd: str, kind: str) -> None:
        if cmd not in seen:
            seen.add(cmd)
            out.append((cmd, kind))

    try:
        # Node / JS / TS — prefer the project's own scripts; fill gaps with direct tools.
        scripts: dict = {}
        pj = os.path.join(root, "package.json")
        if os.path.isfile(pj):
            with open(pj, errors="replace") as f:
                scripts = (json.load(f).get("scripts") or {})
            for key in ("lint", "typecheck", "check"):
                if key in scripts:
                    add(f"npm run {key}", FAST)
            if "test" in scripts:
                add("npm run test", TESTS)
        if os.path.isfile(os.path.join(root, "tsconfig.json")) and not ({"typecheck", "check"} & set(scripts)):
            add("tsc --noEmit", FAST)
        if _has_eslint_config(root) and "lint" not in scripts:
            add("eslint .", FAST)

        # Python
        ppt = os.path.join(root, "pyproject.toml")
        is_py = os.path.isfile(ppt) or os.path.isfile(os.path.join(root, "setup.py"))
        if os.path.isfile(ppt) and "ruff" in _load_toml(ppt).get("tool", {}):
            add("ruff check .", FAST)
        if is_py and os.path.isdir(os.path.join(root, "tests")):
            add("pytest", TESTS)

        # Dart / Flutter
        if os.path.isfile(os.path.join(root, "pubspec.yaml")):
            add("dart analyze", FAST)
            add("dart test" if os.path.isdir(os.path.join(root, "test")) else "flutter test", TESTS)

        # Go
        if os.path.isfile(os.path.join(root, "go.mod")):
            add("go build ./...", FAST)
            add("go test ./...", TESTS)

        # Swift (SwiftPM only; Xcode xcodebuild out of scope)
        if os.path.isfile(os.path.join(root, "Package.swift")):
            add("swift build", FAST)
            add("swift test", TESTS)

        # Kotlin / Gradle — `check` is test-inclusive (dependsOn test): one command, kind=tests.
        if os.path.isfile(os.path.join(root, "build.gradle.kts")) or os.path.isfile(os.path.join(root, "build.gradle")):
            gradle = "./gradlew" if os.path.isfile(os.path.join(root, "gradlew")) else "gradle"
            add(f"{gradle} check", TESTS)
    except Exception:
        pass
    return out


def discover_or_override(root: str = ".") -> list[tuple[str, str]]:
    """TWOB_VERIFY_CMD (';;'- or newline-separated) tagged via classify(), if set; else
    discover_checks(root). The escape hatch for stacks 2B can't auto-detect."""
    raw = os.environ.get("TWOB_VERIFY_CMD")
    if raw:
        cmds = [c.strip() for c in re.split(r";;|\n", raw) if c.strip()]
        return [(c, classify(c)) for c in cmds]
    return discover_checks(root)
```

- [ ] **Step 4: Keep the existing nudge caller working** (transitional; replaced in Task 3). In `src/two_b/orchestrator.py`:
  - Line ~1493: `repo_checks = verify.discover_checks(os.getcwd()) if not os.environ.get("TWOB_NO_VERIFY") else []` — leave as-is (now returns tuples).
  - Line ~1593: change `', '.join(repo_checks[:3])` to `', '.join(c for c, _ in repo_checks[:3])` so the f-string doesn't format tuples.

- [ ] **Step 5: Run tests — expect PASS**

Run: `.venv/bin/python -m unittest tests.test_verify_checks -v`
Expected: PASS (11 tests).

- [ ] **Step 6: Regression** — `.venv/bin/python -m unittest tests.test_verify tests.test_turn_closure 2>&1 | grep -E "^(Ran|OK|FAILED)"` → OK.

- [ ] **Step 7: Commit**

```bash
git add src/two_b/verify.py src/two_b/orchestrator.py tests/test_verify_checks.py
git commit -m "feat(verify): tiered (cmd,kind) checks + Go/Swift/Kotlin/TS detection + override"
```

---

### Task 2: `verify.py` — cancellable `run_checks` runner

**Files:**
- Modify: `src/two_b/verify.py` (add `CheckResult`, `run_checks`, `_truncate`, `VERIFY_TIMEOUT`)
- Test: `tests/test_verify_run.py` (create)

**Interfaces:**
- Consumes: `tools._run_cancellable`, `tools._child_env` (Task-independent, already exist).
- Produces: `CheckResult(cmd:str, status:str, output:str)` with `status ∈ {"pass","fail","skipped","cancelled"}`; `run_checks(checks, *, cancel=None, per_cmd_timeout=VERIFY_TIMEOUT, on_start=None) -> list[CheckResult]`.

- [ ] **Step 1: Write the failing test** — `tests/test_verify_run.py`:

```python
"""verify.run_checks — host-run checks with pass/fail/skipped/cancelled + progress.
Run: `python -m unittest tests.test_verify_run` from the repo root.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import verify  # noqa: E402


class RunChecks(unittest.TestCase):
    def test_pass(self):
        r = verify.run_checks([("sh -c 'exit 0'", "fast")])
        self.assertEqual([(x.cmd, x.status) for x in r], [("sh -c 'exit 0'", "pass")])

    def test_fail_captures_output(self):
        r = verify.run_checks([("sh -c 'echo boom; exit 1'", "tests")])
        self.assertEqual(r[0].status, "fail")
        self.assertIn("boom", r[0].output)

    def test_missing_binary_skipped_not_failed(self):
        r = verify.run_checks([("definitely-no-such-bin-xyz build", "fast")])
        self.assertEqual(r[0].status, "skipped")

    def test_on_start_fires_per_command(self):
        seen = []
        verify.run_checks([("sh -c 'exit 0'", "fast")], on_start=seen.append)
        self.assertEqual(seen, ["sh -c 'exit 0'"])

    def test_preset_cancel_returns_cancelled(self):
        ev = threading.Event()
        ev.set()
        r = verify.run_checks([("sh -c 'exit 0'", "fast")], cancel=ev)
        self.assertEqual(r[0].status, "cancelled")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — expect FAIL** (`run_checks`/`CheckResult` missing).

Run: `.venv/bin/python -m unittest tests.test_verify_run -v` → FAIL.

- [ ] **Step 3: Implement** — add to `src/two_b/verify.py` (add `import shlex` and `from dataclasses import dataclass` to the imports):

```python
VERIFY_TIMEOUT = 300   # per-command cap (seconds); a hung suite can't wedge the session


@dataclass(frozen=True, slots=True)
class CheckResult:
    cmd: str
    status: str   # "pass" | "fail" | "skipped" | "cancelled"
    output: str


def _truncate(s: str, cap: int = 4000) -> str:
    """Keep head + tail (compiler errors lead, test summaries trail)."""
    if len(s) <= cap:
        return s
    return f"{s[: cap * 2 // 3]}\n… [check output truncated] …\n{s[-cap // 3:]}"


def run_checks(checks, *, cancel=None, per_cmd_timeout: int = VERIFY_TIMEOUT, on_start=None):
    """Run each (cmd, kind) check host-side, returning a CheckResult per command. Missing
    binaries are skipped (not failed); a set `cancel` (task.cancel_flag) aborts promptly with
    `cancelled`. Never raises."""
    from . import tools
    results = []
    for cmd, _kind in checks:
        if cancel is not None and cancel.is_set():
            results.append(CheckResult(cmd, "cancelled", ""))
            break
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = cmd.split()
        first = parts[0] if parts else ""
        # shell=True returns exit 127 for a missing binary (no FileNotFoundError) — precheck.
        if not first or shutil.which(first) is None:
            results.append(CheckResult(cmd, "skipped", ""))
            continue
        if on_start:
            on_start(cmd)
        try:
            rc, out, status = tools._run_cancellable(cmd, shell=True, timeout=per_cmd_timeout,
                                                     cancel=cancel, env=tools._child_env())
        except Exception as e:
            results.append(CheckResult(cmd, "fail", f"could not run: {e}"))
            continue
        if status == "cancelled":
            results.append(CheckResult(cmd, "cancelled", ""))
            break
        if status in ("timeout", "kill_failed"):
            results.append(CheckResult(cmd, "fail", _truncate(out) + f"\n[{status} after {per_cmd_timeout}s]"))
            continue
        results.append(CheckResult(cmd, "pass" if rc == 0 else "fail", _truncate(out)))
    return results
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `.venv/bin/python -m unittest tests.test_verify_run -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/two_b/verify.py tests/test_verify_run.py
git commit -m "feat(verify): cancellable host-run runner (pass/fail/skipped/cancelled)"
```

---

### Task 3: `orchestrator.py` — the verify-and-fix loop

**Files:**
- Modify: `src/two_b/orchestrator.py` — import `untrusted`; add `MAX_VERIFY_ROUNDS`; add `verify_rounds` counter; change `repo_checks` to `discover_or_override`; replace the verify-nudge block (~1584-1595) with the loop.
- Test: `tests/test_verify_loop.py` (create)

**Interfaces:**
- Consumes: `verify.discover_or_override`, `verify.run_checks` (Tasks 1-2), `untrusted.wrap`, `_finish_stopped`.

- [ ] **Step 1: Write the failing test** — `tests/test_verify_loop.py` (predicate-level, matching the nudge-test precedent; verifies the fast-filter and round decision helper):

```python
"""Verify-loop decision helpers: fast-only tier filter + round/finish decision.
Run: `python -m unittest tests.test_verify_loop` from the repo root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator as O  # noqa: E402
from two_b.verify import CheckResult  # noqa: E402


class VerifyLoop(unittest.TestCase):
    def test_fast_filter_drops_tests_tier(self):
        checks = [("dart analyze", "fast"), ("flutter test", "tests")]
        self.assertEqual(O._verify_to_run(checks, fast_only=True), [("dart analyze", "fast")])
        self.assertEqual(O._verify_to_run(checks, fast_only=False), checks)

    def test_verdict_pass(self):
        rs = [CheckResult("dart analyze", "pass", "")]
        self.assertEqual(O._verify_verdict(rs), "pass")

    def test_verdict_fail(self):
        rs = [CheckResult("dart analyze", "pass", ""), CheckResult("flutter test", "fail", "x")]
        self.assertEqual(O._verify_verdict(rs), "fail")

    def test_verdict_cancelled_wins(self):
        rs = [CheckResult("dart analyze", "fail", "x"), CheckResult("flutter test", "cancelled", "")]
        self.assertEqual(O._verify_verdict(rs), "cancelled")

    def test_verdict_only_skips_is_pass(self):
        rs = [CheckResult("eslint .", "skipped", "")]
        self.assertEqual(O._verify_verdict(rs), "pass")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — expect FAIL** (`_verify_to_run`/`_verify_verdict` missing). `.venv/bin/python -m unittest tests.test_verify_loop -v`

- [ ] **Step 3: Add helpers + constant + import.** In `src/two_b/orchestrator.py`:
  - Ensure `untrusted` is imported (add to the `from . import ...` line if absent).
  - Near the other module constants (e.g. by `COMPACT_KEEP_TAIL`), add: `MAX_VERIFY_ROUNDS = 2`
  - Add these helpers next to `_edits_all_failed`:

```python
def _verify_to_run(checks, fast_only: bool):
    """The checks to run this round — drop the test tier under TWOB_VERIFY_FAST."""
    return [(c, k) for c, k in checks if not (fast_only and k == "tests")]


def _verify_verdict(results) -> str:
    """'cancelled' if any check was aborted (ESC — stop the task), else 'fail' if any failed,
    else 'pass' (only passes/skips)."""
    if any(r.status == "cancelled" for r in results):
        return "cancelled"
    if any(r.status == "fail" for r in results):
        return "fail"
    return "pass"
```

- [ ] **Step 4: Change `repo_checks` + counter.** In `run_task`:
  - Line ~1486: replace `verify_nudged = False  # ...` with `verify_rounds = 0   # host-run verify-and-fix rounds, bounded by MAX_VERIFY_ROUNDS`.
  - Line ~1493: `repo_checks = verify.discover_or_override(os.getcwd()) if not os.environ.get("TWOB_NO_VERIFY") else []`

- [ ] **Step 5: Replace the verify-nudge block with the loop.** Replace the block at ~1584-1595 (the `# Done-verify (once): ...` comment through its `continue`) with:

```python
                # Host-run verify-and-fix: the model finished with edits that landed — run the
                # project's own checks and, on failure, feed the errors back for a bounded fix
                # loop. The HOST runs them (not the model), so local models get toolchain
                # grounding without run_command. Replaces the old cloud-only verify nudge.
                if content and repo_checks and task.edit_history:
                    to_run = _verify_to_run(repo_checks, bool(os.environ.get("TWOB_VERIFY_FAST")))
                    if to_run:
                        task.status_line = "Verifying"
                        results = verify.run_checks(
                            to_run, cancel=task.cancel_flag,
                            on_start=lambda c: on_event(AgentEvent(EventType.LOG, task.id,
                                                                   {"text": f"Verifying — running {c}…"})))
                        verdict = _verify_verdict(results)
                        if verdict == "cancelled" or task.cancel_flag.is_set():
                            _finish_stopped(task, on_event)
                            return
                        if verdict == "fail":
                            failures = [r for r in results if r.status == "fail"]
                            if verify_rounds < MAX_VERIFY_ROUNDS:
                                verify_rounds += 1
                                body = "\n\n".join(
                                    f"`{r.cmd}` failed:\n" + untrusted.wrap(r.output, f"check:{r.cmd}")
                                    for r in failures)
                                conv.append(msg)
                                conv.append(Message.user(
                                    "Your edits did not pass the project checks. Fix the code so "
                                    "these pass, then finish:\n\n" + body))
                                on_event(AgentEvent(EventType.LOG, task.id,
                                                    {"text": f"{len(failures)} check(s) failed — fixing…"}))
                                continue
                            # Exhausted the fix budget — finish, but report the true state.
                            on_event(AgentEvent(EventType.LOG, task.id, {"text":
                                "⚠ checks still failing after "
                                f"{MAX_VERIFY_ROUNDS} fix attempt(s): "
                                + ", ".join(r.cmd for r in failures)}))
                        else:
                            ran = [r.cmd for r in results if r.status == "pass"]
                            if ran:
                                on_event(AgentEvent(EventType.LOG, task.id,
                                                    {"text": "✓ checks passed: " + ", ".join(ran)}))
```

(Note: no `continue` on the pass/exhausted paths — control falls through to the existing `planparse.finalize_steps(...)` / `TaskState.DONE` below, so the task finishes normally. The false-success guard block right after stays as-is.)

- [ ] **Step 6: Run tests — expect PASS**

Run: `.venv/bin/python -m unittest tests.test_verify_loop -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Full suite** — `.venv/bin/python -m unittest discover -s tests -p 'test_*.py' 2>&1 | grep -E "^(Ran|OK|FAILED)|^(FAIL|ERROR):"` → OK. (If a pre-existing `test_stale_edit` env flake appears, confirm it's unrelated per prior sessions.)

- [ ] **Step 8: Commit**

```bash
git add src/two_b/orchestrator.py tests/test_verify_loop.py
git commit -m "feat: host-run verify-and-fix loop (local+cloud), replaces cloud-only nudge"
```

---

### Task 4: Document the env vars

**Files:**
- Modify: `README.md` (the Configuration / License & privacy area), `PRIVACY.md` (§2/§3 mention host-run checks + the new env vars).

- [ ] **Step 1: README** — under Configuration, add: 2B runs the project's own checks after edits (host-side, language-agnostic) and feeds failures back for a bounded fix loop. Controls: `TWOB_NO_VERIFY=1` (off), `TWOB_VERIFY_FAST=1` (skip test suites, static checks only), `TWOB_VERIFY_CMD="cmd1;;cmd2"` (declare your own checks for unsupported/custom stacks).

- [ ] **Step 2: PRIVACY.md** — add a line: after edits, 2B runs the project's discovered check commands (e.g. `dart analyze`, `go test`, `npm run test`) as local subprocesses on your machine — nothing is sent anywhere; opt out with `TWOB_NO_VERIFY=1`.

- [ ] **Step 3: Commit**

```bash
git add README.md PRIVACY.md
git commit -m "docs: document host-run verify loop + TWOB_VERIFY_* controls"
```

---

## Self-review notes
- **Spec coverage:** §0 detectors → Task 1 (Go/Swift/Kotlin/TS-eslint, tiered, Kotlin single test-inclusive `check`); §1 tiers/classify/override → Task 1; §2 runner (which-precheck, cancelled, head+tail, on_start) → Task 2; §3 loop (fast filter, all-failures fenced feedback, cancel→stop, bounded rounds, honest exhaustion) → Task 3; §5 env vars → Tasks 3-4.
- **Type consistency:** `discover_checks`/`discover_or_override` → `list[tuple[str,str]]`; `CheckResult(cmd,status,output)`; `run_checks(checks, *, cancel, per_cmd_timeout, on_start)`; `_verify_to_run(checks, fast_only)`; `_verify_verdict(results)->str` used consistently across tasks.
- **No-break transition:** Task 1 keeps the old nudge caller compiling against the new tuple type; Task 3 replaces it.
