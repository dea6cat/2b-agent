# Local-Model Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rescue the installed local models that 2B currently *silently no-ops* — models that emit tool calls as text instead of the native `tool_calls` field, and models that stall by narrating intent without acting — without touching the frozen tool schema.

**Architecture:** All changes are host-side. New pure logic lands in `tools.py` (text-recovery + a tolerant JSON parser it uses internally); the turn loop in `orchestrator.py` gains one more bounded nudge. No TUI wiring changes, no schema changes.

**Tech Stack:** Python 3.11–3.14, stdlib only (`json`, `re`), `unittest`. Native Ollama `/api/chat`.

## Measurement findings (2026-07-06) — the empirical basis for this plan

Drove the **real** `orchestrator.run_task` through 7 tasks across two real projects
(`a2_core_package` Dart, `A2_files` mixed) against every installed model, instrumenting the
provider seam. Driver + raw results: `<scratchpad>/measure/driver.py`, `results.*.jsonl`.

| Model | Native tool calls | Calls emitted as **text** (Task 1) | Malformed args (dropped Task 2) |
|---|---|---|---|
| qwen3:8b | 14 | **0** | 0 |
| ornith:9b | 48 | **0** | 0 |
| qwen3.5:9b | 6 | **0** (but see stall below) | 0 |
| **qwen2.5-coder:14b** | **0** | **12 — every call** | 0 |

Conclusions that shaped this plan:

1. **`qwen2.5-coder:14b` makes ZERO native tool calls** — it emits every call as a fenced
   ` ```json ` block, which 2B treats as a final answer. That model does **nothing** across all
   7 tasks today while reporting "done." Recovering those calls (Task 1) is the one clearly
   justified, high-impact fix. The other three models are clean native tool-callers and get ~0%
   benefit from Task 1.
2. **Malformed-arg repair had ZERO measured occurrences** across all 4 models — Ollama pre-parses
   function args to `dict`, so the string branch is never hit, and even the text-JSON blobs were
   well-formed. The standalone "tolerant JSON" task is **dropped**; a tolerant parser survives only
   as a small internal helper for Task 1's text recovery (cheap insurance, no separate wiring).
3. **`qwen3.5:9b` stalls on intent-only prose** ("I'll help you… Let me first explore the project
   structure") — no tool named, no JSON, task ends having done nothing. The existing promise-nudge
   misses it (needs a literal tool name). Task 2 (new) handles this.
4. **Stream resilience and Spanish steering were not exercised** (0 disconnects; English prompts).
   They remain as clearly-labelled optional/insurance work, not justified by this data.

Sample size is small (7 tasks, ~80 model turns total); this measures failure-mode *frequency*,
not end-to-end correctness. But the qwen2.5-coder signal (0/12 native) is unambiguous.

## Global Constraints

- **Frozen tool schema.** Do NOT touch `tools.TOOLS` shape or `toolspec.py` serialization (it asserts byte-identity to `tools.TOOLS` at import — keep it green).
- **Host-side only.** No new model-facing tools; no wire-schema changes.
- **Stdlib only.** No new dependencies.
- **Branch off `main` first:** `git fetch origin && git checkout -b feat/local-model-reliability origin/main`. NEVER commit phase work to `main`.
- **Commits as Alexander.** No `Co-Authored-By: Claude` trailer. Neutral metadata — no competitor/comparison names in messages, branches, or filenames. Keep backticks out of `git -m` messages (they get shell-executed); the messages below are backtick-free by design.
- **Test baseline:** `PYTHONPATH=src <VENV> -m unittest discover -s tests -p "test_*.py"` must stay green (**616 passing**; 5 Linux-gated bwrap tests skip on macOS). `<VENV>` = `/Users/do519-lap/.local/share/uv/tools/2b-agent/bin/python3`.
- **Per-task test shorthand:** `PYTHONPATH=src <VENV> -m unittest tests.<module> -v`.

---

## File Structure

- `src/two_b/tools.py` — **add** two pure helpers next to `coerce_tool_args`: `loads_tolerant()` (internal, used by recovery) and `recover_toolcalls()`. Provider-free, so no import cycle.
- `src/two_b/providers/ollama.py` — call `recover_toolcalls` from `send`/`stream` when native `tool_calls` is empty.
- `src/two_b/orchestrator.py` — add one bounded intent-stall nudge in the turn loop.
- `tests/` — one new test module per task, mirroring `test_toolcall_repair.py` style.

---

### Task 1: Recover tool calls emitted as text (the justified win)

**Problem (measured):** `qwen2.5-coder:14b` emits every tool call as a ```json block in its message
*content*; 2B reads only the native `tool_calls` field (`ollama.py:213,250`), so `msg.tool_calls`
is empty and the turn is treated as a final answer (`orchestrator.py:1426`) — nothing executes.
The promise-nudge cannot save it (a bare JSON blob has no first-person phrasing).

**Files:**
- Modify: `src/two_b/tools.py` (add `loads_tolerant` and `recover_toolcalls` after `coerce_tool_args`, ~line 929)
- Modify: `src/two_b/providers/ollama.py:212-217` (`send`) and `:250-258` (`stream`)
- Test: `tests/test_toolcall_text_recovery.py`

**Interfaces:**
- Consumes: `coerce_tool_args(name, args, known) -> tuple[str, dict]`, `_NAME_KEYS` (existing in `tools.py`); `ToolCall.new(name, arguments)` (already imported in `ollama.py`).
- Produces: `recover_toolcalls(text: str, known) -> list[tuple[str, dict]]` — a list of `(tool_name, args_dict)` for each JSON object in `text` (fenced block, or whole-body JSON) whose tool name (outer or wrapped) is in `known`; `[]` if none. Pure. `loads_tolerant(s: str)` — `json.loads` with a conservative repair pass (trailing commas, Python literals, one level of unclosed brace); returns the parsed value or `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_toolcall_text_recovery.py
import unittest
from two_b.tools import recover_toolcalls, loads_tolerant

KNOWN = ("read_file", "edit_file", "write_file", "search_files", "list_files", "run_git")


class RecoverToolcallsTest(unittest.TestCase):
    def test_fenced_json_block_from_qwen_coder(self):
        # Verbatim shape observed from qwen2.5-coder:14b in the measurement run.
        text = ('1. List all Dart source files under lib/.\n\n'
                '```json\n{\n  "name": "list_files",\n  "arguments": {\n    "path": "lib/"\n  }\n}\n```')
        self.assertEqual(recover_toolcalls(text, KNOWN), [("list_files", {"path": "lib/"})])

    def test_multiple_fenced_calls_in_one_message(self):
        text = ('```json\n{"name": "list_files", "arguments": {"path": "."}}\n```\n'
                '```json\n{"name": "read_file", "arguments": {"path": "README.md"}}\n```')
        self.assertEqual(recover_toolcalls(text, KNOWN),
                         [("list_files", {"path": "."}), ("read_file", {"path": "README.md"})])

    def test_whole_message_is_a_bare_json_call(self):
        self.assertEqual(recover_toolcalls('{"name": "search_files", "arguments": {"query": "TODO"}}', KNOWN),
                         [("search_files", {"query": "TODO"})])

    def test_name_key_variant_and_nested_arg_key(self):
        self.assertEqual(recover_toolcalls('{"tool": "read_file", "input": {"path": "b.py"}}', KNOWN),
                         [("read_file", {"path": "b.py"})])

    def test_unknown_tool_ignored(self):
        self.assertEqual(recover_toolcalls('{"name": "delete_everything", "arguments": {}}', KNOWN), [])

    def test_ordinary_prose_untouched(self):
        self.assertEqual(recover_toolcalls("I'll read the file and report back.", KNOWN), [])

    def test_empty(self):
        self.assertEqual(recover_toolcalls("", KNOWN), [])


class LoadsTolerantTest(unittest.TestCase):
    def test_valid_unchanged(self):
        self.assertEqual(loads_tolerant('{"path": "a.py"}'), {"path": "a.py"})

    def test_trailing_comma(self):
        self.assertEqual(loads_tolerant('{"path": "a.py",}'), {"path": "a.py"})

    def test_unclosed_object(self):
        self.assertEqual(loads_tolerant('{"path": "a.py"'), {"path": "a.py"})

    def test_hopeless_is_none(self):
        self.assertIsNone(loads_tolerant("not json"))
        self.assertIsNone(loads_tolerant(""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src <VENV> -m unittest tests.test_toolcall_text_recovery -v`
Expected: FAIL with `ImportError: cannot import name 'recover_toolcalls'`.

- [ ] **Step 3: Implement `loads_tolerant` and `recover_toolcalls` in `tools.py`**

Add after `coerce_tool_args` (after line 929). `re` and `json` are already imported at the top.

```python
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_PY_LITERALS = ((r"\bTrue\b", "true"), (r"\bFalse\b", "false"), (r"\bNone\b", "null"))
# A ```json … ``` (or ```tool_call …```) fenced block wrapping a JSON object/array.
_FENCE_RE = re.compile(
    r"```(?:json|tool_call|tool_calls)?\s*\n?(\{.*?\}|\[.*?\])\s*```",
    re.DOTALL | re.IGNORECASE,
)


def loads_tolerant(s: str):
    """json.loads with a conservative repair pass (trailing commas, Python literals
    True/False/None, one level of unclosed brace/bracket). Returns the parsed value or
    None. Never raises. Repairs only apply after a strict parse fails, so valid JSON is
    never altered. A cheap safety net for text-emitted tool-call blobs (see recover_toolcalls)."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass
    repaired = _TRAILING_COMMA_RE.sub(r"\1", s)
    for pat, repl in _PY_LITERALS:
        repaired = re.sub(pat, repl, repaired)
    for _ in range(3):
        try:
            return json.loads(repaired)
        except (ValueError, TypeError):
            opens = repaired.count("{") - repaired.count("}")
            brackets = repaired.count("[") - repaired.count("]")
            if opens <= 0 and brackets <= 0:
                return None
            repaired = repaired + ("}" if opens > 0 else "]")
    return None


def recover_toolcalls(text: str, known) -> list[tuple[str, dict]]:
    """Recover tool calls a model emitted as JSON in its message text instead of the native
    tool_calls field (measured: qwen2.5-coder:14b does this for 100% of its calls). Returns a
    list of (name, args) for each JSON object — in a fenced code block, or the whole message
    body — whose tool name (outer, or wrapped via coerce_tool_args) is in `known`; [] if none.
    Pure. Bare JSON embedded mid-prose is intentionally not scanned (too risky); it is still
    recovered when the whole body is JSON."""
    if not text or not any(k in text for k in known):
        return []
    calls: list[tuple[str, dict]] = []
    blobs = [m.group(1) for m in _FENCE_RE.finditer(text)]
    stripped = text.strip()
    if stripped[:1] in "{[":
        blobs.append(stripped)
    for blob in blobs:
        obj = loads_tolerant(blob)
        if obj is None:
            continue
        for item in (obj if isinstance(obj, list) else [obj]):
            if not isinstance(item, dict):
                continue
            name = ""
            for nk in _NAME_KEYS:
                v = item.get(nk)
                if isinstance(v, str) and v.strip():
                    name = v.strip()
                    break
            cname, cargs = coerce_tool_args(name, item, tuple(known))
            if cname in known:
                calls.append((cname, cargs))
    return calls
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src <VENV> -m unittest tests.test_toolcall_text_recovery -v`
Expected: PASS (11 tests).

- [ ] **Step 5: Wire recovery into `ollama.send` and `ollama.stream`**

Add the import at `providers/ollama.py:16` region:

```python
from ..tools import recover_toolcalls
```

In `send`, after `text = (msg.get("content") or "").strip()` (line 217), insert:

```python
        if not calls and text:
            recovered = recover_toolcalls(text, [t.name for t in tools])
            calls = [ToolCall.new(name=n, arguments=a) for n, a in recovered]
```

In `stream`, after `text = "".join(content).strip()` (line 258), insert the same three lines.

> NOTE (document, do not fix here): in the streaming path the raw JSON was already emitted to
> the UI via `on_text` before recovery runs, so the user briefly sees the blob. Recovery still
> promotes the call so it *executes* — the correctness win. Buffering a suspected tool-call blob
> before streaming it is out of scope (see Deferred).

- [ ] **Step 6: Verify no import cycle and full suite green**

Run: `PYTHONPATH=src <VENV> -c "import two_b.providers.ollama"` → Expected: exit 0 (no cycle: `tools.py` imports only `seatbelt, untrusted`).
Run: `PYTHONPATH=src <VENV> -m unittest discover -s tests -p "test_*.py"` → Expected: OK (627 = 616 + 11).

- [ ] **Step 7: Commit**

```bash
git add src/two_b/tools.py src/two_b/providers/ollama.py tests/test_toolcall_text_recovery.py
git commit -m "feat: recover tool calls a local model emits as text instead of native tool_calls"
```

---

### Task 2: Nudge an intent-only stall (the qwen3.5:9b case)

**Problem (measured):** `qwen3.5:9b` sometimes returns a no-tool-call turn that only narrates intent
("I'll help you… Let me first explore the project structure to locate these files") — no literal
tool name, no JSON. `_promised_tool_but_didnt` (`orchestrator.py:322`) needs a literal tool name, so
it doesn't fire, and Task 1 finds no JSON. The task finalizes as DONE having taken zero actions.

**Files:**
- Modify: `src/two_b/orchestrator.py` (add `_STALL_NUDGE` + `_stalled_without_acting` near `_promised_tool_but_didnt` ~line 333; add a counter and one nudge branch in `run_task`)
- Test: `tests/test_stall_nudge.py`

**Interfaces:**
- Consumes: `_INTENT_RE` (existing, `orchestrator.py:306`).
- Produces: `_stalled_without_acting(text: str) -> bool` — True if a no-tool-call turn is forward-intent narration; used only when the task has made zero tool calls, so a genuine done-report (which follows real actions) is never flagged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stall_nudge.py
import unittest
from two_b import orchestrator as O


class StallDetectTest(unittest.TestCase):
    def test_intent_without_tool_name_is_a_stall(self):
        self.assertTrue(O._stalled_without_acting(
            "I'll help you understand this package. Let me first explore the project structure."))
        self.assertTrue(O._stalled_without_acting("Let me look at the files to understand this."))

    def test_delivered_answer_is_not_a_stall(self):
        # No forward-intent phrasing -> a real answer, must not be flagged.
        self.assertFalse(O._stalled_without_acting(
            "The package is a Dart agent framework. It exports three classes."))

    def test_empty(self):
        self.assertFalse(O._stalled_without_acting(""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src <VENV> -m unittest tests.test_stall_nudge -v`
Expected: FAIL with `AttributeError: module 'two_b.orchestrator' has no attribute '_stalled_without_acting'`.

- [ ] **Step 3: Implement the detector + nudge message**

Add after `_promised_tool_but_didnt` (after line 332):

```python
_STALL_NUDGE = (
    "You described what you intend to do but didn't use any tool. Investigate or act with a "
    "tool now (list_files, read_file, search_files, edit_file, …) — don't only narrate the plan. "
    "If you already have the answer, give it plainly without describing steps."
)


def _stalled_without_acting(text: str) -> bool:
    """True if a no-tool-call turn is forward-intent narration ('I'll…', 'let me explore…')
    rather than a delivered answer. Caller gates this on zero tool calls made so far, so a
    genuine done-report (which comes after real actions) is never flagged."""
    return bool(text) and bool(_INTENT_RE.search(text))
```

- [ ] **Step 4: Track actions taken and add the bounded nudge in `run_task`**

Near the other per-task counters (`promise_nudges = 0`, ~line 1358), add:

```python
        tool_calls_made = 0    # any tool calls dispatched this task (gates the intent-stall nudge)
        stall_nudges = 0       # intent-only stall nudge fires at most once
```

In the no-tool-calls branch, immediately after the promise-nudge `if` block (after line 1434, before the done-verify block), add:

```python
                # A no-tool-call turn that only narrates intent, with zero actions taken so far,
                # is a stall (measured on qwen3.5:9b) — nudge once to actually use a tool. Bounded,
                # and gated on tool_calls_made==0 so a real final answer is never nudged.
                if (stall_nudges < 1 and tool_calls_made == 0
                        and not _promised_tool_but_didnt(content)
                        and _stalled_without_acting(content)):
                    stall_nudges += 1
                    conv.append(msg)
                    conv.append(Message.user(_STALL_NUDGE))
                    continue
```

After the tool-call coercion loop (after line 1471, `tc.name, tc.arguments = ...`), add:

```python
            tool_calls_made += len(calls)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `PYTHONPATH=src <VENV> -m unittest tests.test_stall_nudge -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Full suite green**

Run: `PYTHONPATH=src <VENV> -m unittest discover -s tests -p "test_*.py"` → Expected: OK (630 = 627 + 3).

- [ ] **Step 7: Commit**

```bash
git add src/two_b/orchestrator.py tests/test_stall_nudge.py
git commit -m "feat: nudge a model that narrates intent but takes no action to actually use a tool"
```

---

## Optional / insurance (NOT justified by current measurements)

These address real code gaps but had **zero measured occurrences** in the cross-model run. Build
only if a future measurement surfaces them, or if you want defensive insurance. Keep each as its
own branch/phase; do not bundle into the Task 1–2 phase.

### Optional A — Stream resilience (rare-event insurance)

`post_stream`'s iteration (`base.py:98-100`) and the NDJSON parse (`ollama.py:243`) run outside the
retryable-error guard, so a mid-stream disconnect or one malformed line aborts the turn (non-retryable
→ `_finish_failed`). Not observed (0 disconnects in the run), but cheap insurance for slow/killed local
servers. Fix: `import http.client` in `base.py`; wrap the `for raw in resp` loop and re-raise transport
errors as `ProviderError(retryable=True)`; guard `_json.loads(line)` with `try/except ValueError: continue`;
and in `ollama.stream` downgrade to `retryable=False` once visible content has streamed (so a retry can't
duplicate shown text). Full test code and diffs are preserved in git history of this file (commit
introducing the plan) if needed.

### Optional B — Spanish steering (language-dependent, binary)

`_INTENT_RE` (`orchestrator.py:306`) and `_DANGLING_RE` (`:666`) are English-only, while the system
prompt tells the model to reply in the user's language. Not exercised (English prompts), but relevant
given the user's Spanish locale: a model that says "Voy a usar edit_file…" and stops is not nudged, and
Spanish back-references never trigger archive recall. Fix: extend both regexes with Spanish
future-intent / back-reference vocabulary (`voy a`, `déjame`, `usaré`; `antes`, `ya`, `ese archivo`,
`mencionaste`, `vuelve a`). Note: Task 2's `_stalled_without_acting` inherits any `_INTENT_RE` widening
for free.

### Optional C — KV-cache-type / flash-attention advisory (throughput, opt-in)

Validated separately: `_kv_bytes_per_token` (`ollama.py:64`) assumes f16 KV, so it under-sizes `num_ctx`
when the server runs quantized KV — but **only when RAM is the binding constraint** (measured: zero gain
for qwen3:8b, which is trained-ceiling-bound; up to ~2× context for RAM-bound 9B models). 2B cannot detect
the server's KV type from `/api/show`, so this must be **opt-in**, never guessed. Fix: a `TWOB_KV_CACHE_TYPE`
env that scales the per-token estimate in `_compute_ctx`, plus a `2b --doctor` advisory recommending
`OLLAMA_FLASH_ATTENTION=1` / `OLLAMA_KV_CACHE_TYPE=q8_0`. Speed only; does not affect task reliability.

---

## End-of-Phase Gate (run after Task 2, before merge)

- [ ] **Adversarial review:** dispatch the `codeObserver` subagent on the staged diff (`git diff main...HEAD`). Fix CRITICAL/HIGH with regression tests. Focus: `recover_toolcalls` must not promote JSON from ordinary prose (false execution), and the stall nudge must not fire on genuine one-turn answers.
- [ ] **Full suite green** on Python 3.13 and 3.14.
- [ ] **Real-project validation** — re-run the measurement driver (`<scratchpad>/measure/driver.py`) on `feat/local-model-reliability`. Success criteria, keyed to the baseline table above:
  - **qwen2.5-coder:14b**: its 12 text-emitted calls now execute (native_calls > 0 in the trace; tasks actually list/read/edit instead of no-op'ing). This is the headline before/after.
  - **qwen3:8b / ornith:9b**: unchanged (no regression) — still 0 text-recoveries, tasks still pass.
  - **qwen3.5:9b**: intent-only stalls now receive one nudge and proceed to a tool call.
- [ ] **Commit as Alexander** (verify `git log -1 --format='%an <%ae>%n%(trailers)'` — Alexander, no `Co-Authored-By`).
- [ ] **Merge:** push branch → fast-forward merge to `main` → push `main`. Verify `local == origin`.
- [ ] **Update `docs/roadmap-handoff.md`:** add text-emitted-call recovery + intent-stall nudge to "Shipped"; note the deferred items below.

## Real-project validation harness

- **Dev interpreter:** `/Users/do519-lap/.local/share/uv/tools/2b-agent/bin/python3` with `PYTHONPATH=/Users/do519-lap/repo_apps/2B/src`.
- **Measurement driver:** `<scratchpad>/measure/driver.py <model>` — drives `run_task` in-process over 7 tasks across scratch copies of `a2_core_package` and `A2_files`, instrumenting the provider seam; writes `results.<model>.jsonl`. Re-sync fresh project copies before each model (edits accumulate).
- **Models:** `qwen3:8b`, `ornith:9b`, `qwen3.5:9b`, `qwen2.5-coder:14b`. **macOS gotchas:** `sleep`/`timeout` unavailable; run long model calls in the background; use the scratchpad for temp files.

## Deferred / dropped

- **DROPPED — standalone tolerant-JSON-in-`_parse_args`/`_as_arg_dict` task:** zero measured malformed args (Ollama pre-parses to dict). The tolerant parser survives only inside Task 1's `recover_toolcalls`.
- **Deferred — exec-tool arg unwrap** (`run_git`/`run_command` wrapper shapes): not exercised; needs a guarded narrow-unwrap rule.
- **Deferred — stream-buffering** so a suspected tool-call blob isn't shown before Task 1 strips/executes it.
- **Deferred — fuzzy / line-range edit tier; few-shot exemplar; model-aware compaction** — larger separate phases, not surfaced by this measurement.
