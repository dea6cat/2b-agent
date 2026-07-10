# Reasoning/Thinking Control (`/think`) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A host-side lever over model reasoning — `off/on/low/medium/high` — wired into Ollama (local core) and Google now; Anthropic and OpenAI-compat accept-and-ignore it (deferred). Users control it per session via `/think` and persistently via `TWOB_THINK`.

**Architecture:** `reasoning` is a new keyword arg threaded `orchestrator → stream_with_retry → provider.stream`. The host resolves the effective level once (`_reasoning_effective`: session `/think` > `TWOB_THINK` > `None`), and each provider maps it to its native wire field (Ollama `think`, Google `thinkingConfig.thinkingBudget`). The frozen five-tool schema is untouched — reasoning is a provider-call parameter, not a tool.

**Tech Stack:** Python 3, stdlib only, `unittest`.

**Spec:** `docs/superpowers/specs/2026-07-10-reasoning-control-design.md` (read it — full rationale, per-provider tables, deferred-provider reasoning).

## Global Constraints

- **Stdlib only.** No new dependencies.
- Tests: `unittest`, header `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))`, run with `.venv/bin/python -m unittest tests.test_<name>`. Mirror existing test style.
- **Level vocabulary is exactly** `off · on · low · medium · high`; the sentinel `None` means "no user preference — provider's capped default."
- **Ollama never sends `think` to a non-thinking model** (Ollama errors otherwise) — capability-gate via `/api/show` `capabilities`.
- **Google `thinkingBudget` is never `-1`/dynamic** — always a bounded integer; `None` → capped `MED`.
- **Uniform call site:** all four `provider.stream` signatures accept `reasoning: str | None = None`; deferred providers ignore it. `send()` is untouched (ignores reasoning).
- After each task the **full suite stays green** — no broken intermediate. Fake providers in tests absorb the new kwarg via `**_kwargs`.
- Commit after each task, as the repo user, **no `Co-Authored-By`**, neutral messages. If a message needs backticks, use `git commit -F <file>`.
- Branch: `feat/reasoning-control` (already created off main).

---

### Task 1: Plumbing — thread `reasoning` through the call chain (accept-and-ignore)

Adds the `reasoning` kwarg everywhere so the wiring is uniform and the tree stays green, before any provider gives it meaning. Also finalizes the two **deferred** providers (accept-ignore + `supports_reasoning → False`).

**Files:**
- Modify: `src/two_b/providers/base.py:181` (`stream_with_retry`)
- Modify: `src/two_b/providers/ollama.py` (`stream` signature), `google.py` (`stream` signature), `anthropic.py` (`stream` signature + `supports_reasoning`), `openai_compat.py` (`stream` signature + `supports_reasoning`)
- Modify (fakes absorb the kwarg): `tests/test_archive_inject.py`, `tests/test_cancel_streaming.py`, `tests/test_clarify_nudge.py`, `tests/test_compaction_hardening.py`, `tests/test_continuity.py`, `tests/test_loop_guard.py`, `tests/test_parallel_reads.py`, `tests/test_retry.py`, `tests/test_steer.py`, `tests/test_subagents.py`, `tests/test_turn_closure.py`, `tests/test_verify.py`
- Test: `tests/test_reasoning_plumbing.py` (create)

**Interfaces:**
- Produces: `stream_with_retry(provider, conversation, model, tools, on_text, *, retries=3, cancel=None, reasoning=None)` — forwards `reasoning` to `provider.stream`. Every `provider.stream(..., *, cancel=None, reasoning=None)`. `AnthropicProvider.supports_reasoning(model)->False`, `OpenAICompatProvider.supports_reasoning(model)->False`.

- [ ] **Step 1: Write the failing test** — `tests/test_reasoning_plumbing.py`:

```python
"""stream_with_retry forwards `reasoning` to provider.stream; deferred providers report
supports_reasoning() False. Run: `python -m unittest tests.test_reasoning_plumbing`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers.base import stream_with_retry  # noqa: E402
from two_b.providers.anthropic import AnthropicProvider  # noqa: E402
from two_b.providers.openai_compat import OpenAICompatProvider  # noqa: E402


class _FakeProvider:
    def __init__(self):
        self.seen = {}

    def stream(self, conv, model, tools, on_text, *, cancel=None, reasoning=None):
        self.seen["reasoning"] = reasoning
        from two_b.conversation import Message
        from two_b.providers.base import ProviderResponse
        return ProviderResponse(message=Message.assistant(text="ok"), raw={})


class Plumbing(unittest.TestCase):
    def test_default_reasoning_is_none(self):
        p = _FakeProvider()
        stream_with_retry(p, None, "m", (), lambda _c: None)
        self.assertIsNone(p.seen["reasoning"])

    def test_reasoning_forwarded(self):
        p = _FakeProvider()
        stream_with_retry(p, None, "m", (), lambda _c: None, reasoning="off")
        self.assertEqual(p.seen["reasoning"], "off")

    def test_deferred_providers_report_unsupported(self):
        self.assertFalse(AnthropicProvider().supports_reasoning("claude-opus-4-8"))
        self.assertFalse(OpenAICompatProvider("x", "http://x", "K").supports_reasoning("m"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — expect FAIL** (`stream_with_retry` rejects `reasoning`; `supports_reasoning` missing).

Run: `.venv/bin/python -m unittest tests.test_reasoning_plumbing -v` → FAIL.

- [ ] **Step 3: `base.py` — add + forward the kwarg.** In `src/two_b/providers/base.py`, change the `stream_with_retry` signature (line 181) and the single `provider.stream(...)` call (line 189):

```python
def stream_with_retry(provider, conversation, model, tools, on_text, *, retries=3, cancel=None, reasoning=None):
```
```python
            return provider.stream(conversation, model, tools, on_text, cancel=cancel, reasoning=reasoning)
```

- [ ] **Step 4: All four provider `stream` signatures accept `reasoning`.** Add `, reasoning=None` to the keyword-only part of each:
  - `ollama.py` `def stream(self, conversation, model, tools, on_text, *, cancel=None, reasoning=None):`
  - `google.py` same
  - `anthropic.py` same
  - `openai_compat.py` same

  (Ollama/Google leave the body unchanged for now — they give it meaning in Tasks 2/3. Anthropic/OpenAI-compat ignore it permanently.)

- [ ] **Step 5: `supports_reasoning` on the deferred providers.** In `anthropic.py` (in `AnthropicProvider`) and `openai_compat.py` (in `OpenAICompatProvider`), add:

```python
    def supports_reasoning(self, model: str) -> bool:
        return False   # reasoning deferred (see design §7)
```

- [ ] **Step 6: Fakes absorb the kwarg.** In each of the 12 listed test files, change every fake `stream` signature ending `*, cancel=None` to end `*, cancel=None, **_kwargs`. (Absorbs `reasoning` and any future provider-call kwarg once — no per-kwarg ripple next time.) Example: `def stream(self, conv, model, tools, on_text, *, cancel=None, **_kwargs):`.

- [ ] **Step 7: Run tests — expect PASS**

Run: `.venv/bin/python -m unittest tests.test_reasoning_plumbing -v` → PASS (3 tests).

- [ ] **Step 8: Full suite** — `.venv/bin/python -m unittest discover -s tests -p 'test_*.py' 2>&1 | grep -E "^(Ran|OK|FAILED)|^(FAIL|ERROR):"` → OK. (Confirms no fake was missed.)

- [ ] **Step 9: Commit**

```bash
git add src/two_b/providers/base.py src/two_b/providers/ollama.py src/two_b/providers/google.py src/two_b/providers/anthropic.py src/two_b/providers/openai_compat.py tests/
git commit -m "feat(reasoning): thread reasoning kwarg through stream_with_retry (accept-ignore)"
```

---

### Task 2: Ollama — map `reasoning` to `think` + capability gate

**Files:**
- Modify: `src/two_b/providers/ollama.py` (add `supports_reasoning`, `_think_value`, module constant; set `payload["think"]` in `stream`)
- Test: `tests/test_reasoning_ollama.py` (create)

**Interfaces:**
- Consumes: `self._show(model)` (exists — cached `/api/show`).
- Produces: `OllamaProvider.supports_reasoning(model)->bool`; `OllamaProvider._think_value(model, reasoning)-> bool|str|None`.

- [ ] **Step 1: Write the failing test** — `tests/test_reasoning_ollama.py`:

```python
"""Ollama maps a reasoning level to the `think` field, gated on model capability.
Run: `python -m unittest tests.test_reasoning_ollama`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers.ollama import OllamaProvider  # noqa: E402


def _prov(caps):
    p = OllamaProvider()
    p._show = lambda model: {"capabilities": caps}     # bypass network
    return p


class ThinkValue(unittest.TestCase):
    def test_capable_model_reports_supported(self):
        self.assertTrue(_prov(["completion", "tools", "thinking"]).supports_reasoning("qwen3.5:9b"))
        self.assertFalse(_prov(["completion", "tools"]).supports_reasoning("llama3:8b"))

    def test_none_reasoning_omits_think(self):
        self.assertIsNone(_prov(["thinking"])._think_value("qwen3.5:9b", None))

    def test_off_on(self):
        p = _prov(["thinking"])
        self.assertIs(p._think_value("qwen3.5:9b", "off"), False)
        self.assertIs(p._think_value("qwen3.5:9b", "on"), True)

    def test_level_is_boolean_true_for_ordinary_model(self):
        self.assertIs(_prov(["thinking"])._think_value("qwen3.5:9b", "high"), True)

    def test_level_is_native_string_for_gpt_oss(self):
        self.assertEqual(_prov(["thinking"])._think_value("gpt-oss:20b", "high"), "high")

    def test_noncapable_model_never_gets_think(self):
        p = _prov(["completion", "tools"])
        for lvl in ("off", "on", "low", "medium", "high", None):
            self.assertIsNone(p._think_value("llama3:8b", lvl))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — expect FAIL** (`supports_reasoning`/`_think_value` missing). `.venv/bin/python -m unittest tests.test_reasoning_ollama -v`

- [ ] **Step 3: Implement** in `src/two_b/providers/ollama.py`. Add a module constant near the top (after the imports/other constants):

```python
_THINK_STRING_MODELS = ("gpt-oss",)   # accept think:"low|medium|high"; other thinking models are boolean
```

Add these two methods to `OllamaProvider` (e.g. just after `_show`):

```python
    def supports_reasoning(self, model: str) -> bool:
        """True if the model advertises the 'thinking' capability (from /api/show)."""
        return "thinking" in (self._show(model).get("capabilities") or [])

    def _think_value(self, model: str, reasoning):
        """Map a reasoning level to Ollama's `think` field, or None to omit it. None -> omit
        (model default). A non-thinking model always omits (Ollama errors on `think`). Ordinary
        thinking models take a bool; gpt-oss takes a native string level."""
        if reasoning is None or not self.supports_reasoning(model):
            return None
        if reasoning == "off":
            return False
        if reasoning in ("low", "medium", "high") and any(s in model.lower() for s in _THINK_STRING_MODELS):
            return reasoning
        return True   # "on", or a level on a boolean thinking model
```

In `stream`, after the `payload = {...}` dict is built (before the `post_stream` loop), add:

```python
        think = self._think_value(model, reasoning)
        if think is not None:
            payload["think"] = think
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `.venv/bin/python -m unittest tests.test_reasoning_ollama -v` → PASS (6 tests).

- [ ] **Step 5: Regression** — `.venv/bin/python -m unittest tests.test_reasoning_plumbing tests.test_turn_closure 2>&1 | grep -E "^(Ran|OK|FAILED)"` → OK.

- [ ] **Step 6: Commit**

```bash
git add src/two_b/providers/ollama.py tests/test_reasoning_ollama.py
git commit -m "feat(reasoning): Ollama maps level to think, capability-gated via /api/show"
```

---

### Task 3: Google — map `reasoning` to `thinkingConfig.thinkingBudget`

**Files:**
- Modify: `src/two_b/providers/google.py` (add `supports_reasoning`, budget constants, `_thinking_budget`; add `thinking_budget` param to `_payload`; compute + pass it in `stream`)
- Test: `tests/test_reasoning_google.py` (create)

**Interfaces:**
- Produces: `GoogleProvider.supports_reasoning(model)->bool`; `GoogleProvider._thinking_budget(model, reasoning)-> int|None`; `_payload(conversation, tools, thinking_budget=None)`.

- [ ] **Step 1: Write the failing test** — `tests/test_reasoning_google.py`:

```python
"""Google maps a reasoning level to thinkingConfig.thinkingBudget (bounded, never dynamic).
Run: `python -m unittest tests.test_reasoning_google`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers.google import GoogleProvider, _G_LOW, _G_MED, _G_HIGH  # noqa: E402


class Budget(unittest.TestCase):
    def setUp(self):
        self.p = GoogleProvider()

    def test_capability_by_model(self):
        self.assertTrue(self.p.supports_reasoning("gemini-2.5-flash"))
        self.assertFalse(self.p.supports_reasoning("gemini-2.0-flash"))

    def test_none_is_capped_medium(self):
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", None), _G_MED)

    def test_levels(self):
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", "low"), _G_LOW)
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", "medium"), _G_MED)
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", "on"), _G_MED)
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", "high"), _G_HIGH)

    def test_off_disables_on_flash(self):
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", "off"), 0)

    def test_off_uses_minimum_on_pro(self):
        self.assertEqual(self.p._thinking_budget("gemini-2.5-pro", "off"), 128)

    def test_unsupported_model_omits(self):
        self.assertIsNone(self.p._thinking_budget("gemini-2.0-flash", "high"))

    def test_payload_adds_thinkingconfig_only_when_budget_given(self):
        conv = type("C", (), {"system_prompt": "s", "messages": []})()
        with_b = self.p._payload(conv, (), thinking_budget=_G_MED)
        self.assertEqual(with_b["generationConfig"]["thinkingConfig"]["thinkingBudget"], _G_MED)
        self.assertNotIn("generationConfig", self.p._payload(conv, ()))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — expect FAIL** (`supports_reasoning`/`_thinking_budget`/constants missing; `_payload` has no `thinking_budget`). `.venv/bin/python -m unittest tests.test_reasoning_google -v`

- [ ] **Step 3: Implement** in `src/two_b/providers/google.py`. Add budget constants near `BASE`/`_MODELS` (top of module):

```python
_G_LOW, _G_MED, _G_HIGH = 2048, 8192, 24576   # bounded thinking budgets; never -1 (dynamic)
```

Add these methods to `GoogleProvider`:

```python
    def supports_reasoning(self, model: str) -> bool:
        return model.startswith("gemini-2.5")

    def _thinking_budget(self, model: str, reasoning):
        """thinkingBudget for thinkingConfig, or None to omit it (unsupported model). None ->
        capped MED (protect latency); never -1/dynamic. 2.5 Pro can't fully disable, so 'off'
        uses its minimum."""
        if not self.supports_reasoning(model):
            return None
        if reasoning is None:
            return _G_MED
        tier = {"off": 0, "low": _G_LOW, "medium": _G_MED, "on": _G_MED, "high": _G_HIGH}.get(reasoning, _G_MED)
        if tier == 0 and "pro" in model:
            return 128
        return tier
```

Change `_payload` to accept and apply the budget:

```python
    def _payload(self, conversation: Conversation, tools: tuple[ToolSpec, ...], thinking_budget=None) -> dict:
        p = {
            "systemInstruction": {"parts": [{"text": conversation.system_prompt}]},
            "contents": self._contents(conversation),
            "tools": to_gemini(tools),
        }
        if thinking_budget is not None:
            p["generationConfig"] = {"thinkingConfig": {"thinkingBudget": thinking_budget}}
        return p
```

In `stream`, compute the budget and pass it (the `send` path stays on the default — no thinkingConfig). Replace the `self._payload(conversation, tools)` call inside `stream`'s `post_stream(...)` with a precomputed payload:

```python
        budget = self._thinking_budget(model, reasoning)
        payload = self._payload(conversation, tools, thinking_budget=budget)
        url = f"{BASE}/models/{model}:streamGenerateContent?alt=sse"
        text_parts: list = []
        calls: list = []
        last: dict = {}
        for line in post_stream(url, payload, headers=self._headers(),
                                provider=self.name, cancel=cancel):
```

(The existing `url = ...` line inside `stream` is removed in favor of the one above; the rest of the loop body is unchanged.)

- [ ] **Step 4: Run tests — expect PASS**

Run: `.venv/bin/python -m unittest tests.test_reasoning_google -v` → PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/two_b/providers/google.py tests/test_reasoning_google.py
git commit -m "feat(reasoning): Google maps level to a bounded thinkingBudget"
```

---

### Task 4: Resolver + session field + `/think` command + wiring

**Files:**
- Modify: `src/two_b/session.py` (add `think` field)
- Modify: `src/two_b/orchestrator.py` (add `_THINK_LEVELS`, `_reasoning_effective`; pass `reasoning=` at the two `stream_with_retry` sites `:1553`, `:1785`)
- Modify: `src/two_b/commands.py` (add `@command("think")`)
- Test: `tests/test_reasoning_control.py` (create)

**Interfaces:**
- Consumes: `provider.supports_reasoning(model)` (Tasks 1–3), `registry.resolve`, `orchestrator._reasoning_effective`.
- Produces: `Session.think: str | None`; `orchestrator._reasoning_effective(session)->str|None`; `/think` command.

- [ ] **Step 1: Write the failing test** — `tests/test_reasoning_control.py`:

```python
"""Reasoning resolution precedence (session > TWOB_THINK > None) + Session.think field.
Run: `python -m unittest tests.test_reasoning_control`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator as O  # noqa: E402
from two_b.session import Session  # noqa: E402


class Resolve(unittest.TestCase):
    def setUp(self):
        os.environ.pop("TWOB_THINK", None)
        self.addCleanup(lambda: os.environ.pop("TWOB_THINK", None))

    def test_default_is_none(self):
        self.assertIsNone(O._reasoning_effective(Session()))

    def test_session_override_wins(self):
        s = Session()
        s.think = "off"
        os.environ["TWOB_THINK"] = "high"
        self.assertEqual(O._reasoning_effective(s), "off")

    def test_env_used_when_no_session(self):
        os.environ["TWOB_THINK"] = "low"
        self.assertEqual(O._reasoning_effective(Session()), "low")

    def test_invalid_env_ignored(self):
        os.environ["TWOB_THINK"] = "banana"
        self.assertIsNone(O._reasoning_effective(Session()))

    def test_session_field_defaults_none(self):
        self.assertIsNone(Session().think)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — expect FAIL** (`_reasoning_effective` missing; `Session` has no `think`). `.venv/bin/python -m unittest tests.test_reasoning_control -v`

- [ ] **Step 3: `Session.think` field.** In `src/two_b/session.py`, in the `Session` dataclass (next to `continuity_override`), add:

```python
    think: str | None = None     # /think override: off|on|low|medium|high; None = provider default
```

- [ ] **Step 4: `_reasoning_effective` + constant.** In `src/two_b/orchestrator.py`, near the other module constants add:

```python
_THINK_LEVELS = frozenset({"off", "on", "low", "medium", "high"})
```

and next to `_continuity_effective` add:

```python
def _reasoning_effective(session) -> str | None:
    """The reasoning level for this turn: session /think override, else TWOB_THINK, else None
    (each provider's capped default). Mirrors _continuity_effective's precedence."""
    override = getattr(session, "think", None)
    if override in _THINK_LEVELS:
        return override
    env = os.environ.get("TWOB_THINK", "").strip().lower()
    return env if env in _THINK_LEVELS else None
```

- [ ] **Step 5: Wire the two call sites.** In `run_task`, add `reasoning=_reasoning_effective(session)` to both `stream_with_retry` calls:
  - Line ~1553: `resp = stream_with_retry(provider, req_conv, model, active_specs, on_text, cancel=task.cancel_flag, reasoning=_reasoning_effective(session))`
  - Line ~1785: `resp = stream_with_retry(provider, req_conv, model, _active_specs(is_local), on_final, cancel=task.cancel_flag, reasoning=_reasoning_effective(session))`

  (`session` is already in scope — `_continuity_effective(session, …)` is called earlier in `run_task`.)

- [ ] **Step 6: `/think` command.** In `src/two_b/commands.py`, after the `/continuity` command, add:

```python
_THINK_ALIASES = {"off": "off", "no": "off", "false": "off",
                  "on": "on", "yes": "on", "true": "on",
                  "low": "low", "medium": "medium", "med": "medium", "high": "high"}


@command("think")
def _think(rest, app):
    """Control model reasoning: /think off|on|low|medium|high (bare shows current)."""
    session = app.session
    resolved = registry.resolve(app.registry, session.default_model)
    provider, model = resolved if resolved else (None, "")
    supports = bool(provider and provider.supports_reasoning(model))
    arg = rest.strip().lower()
    if not arg:
        eff = orchestrator._reasoning_effective(session) or "default"
        cap = "supported" if supports else "not supported by the current model"
        app.ui.print(f"Reasoning: [bold]{eff}[/bold] ({cap}).  "
                     "Set with /think off|on|low|medium|high.")
        return
    level = _THINK_ALIASES.get(arg)
    if level is None:
        app.ui.print("[red]Usage:[/red] /think off|on|low|medium|high")
        return
    session.think = level
    note = "" if supports else "  (current model doesn't support reasoning — no effect)"
    app.ui.print(f"Reasoning [bold]{level}[/bold] for this session.{note}")
```

  (`registry` and `orchestrator` are already imported in `commands.py` — used by `/model` and `/continuity`.)

- [ ] **Step 7: Run tests — expect PASS**

Run: `.venv/bin/python -m unittest tests.test_reasoning_control -v` → PASS (5 tests).

- [ ] **Step 8: Full suite** — `.venv/bin/python -m unittest discover -s tests -p 'test_*.py' 2>&1 | grep -E "^(Ran|OK|FAILED)|^(FAIL|ERROR):"` → OK.

- [ ] **Step 9: Commit**

```bash
git add src/two_b/session.py src/two_b/orchestrator.py src/two_b/commands.py tests/test_reasoning_control.py
git commit -m "feat(reasoning): /think command + TWOB_THINK resolver wired into run_task"
```

---

### Task 5: Document `/think` + `TWOB_THINK`

**Files:**
- Modify: `README.md` (Configuration — commands + env toggles), `PRIVACY.md` (only if it enumerates env controls — otherwise skip; reasoning is not a data-flow change)

- [ ] **Step 1: README** — under Configuration, document: `/think off|on|low|medium|high` controls model reasoning per session (off = faster on slow local models); `TWOB_THINK` sets it persistently. Note it applies to reasoning-capable Ollama models and Google (Gemini 2.5); Anthropic and OpenAI-compatible providers don't support it yet. Add `TWOB_THINK` to the `·`-separated environment-toggles list in the same format as the existing entries.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document /think reasoning control + TWOB_THINK"
```

---

## Self-review notes

- **Spec coverage:** §control-model (levels, precedence, default policy) → Task 4 (`_reasoning_effective`) + Tasks 2–3 (per-provider `None`→capped default); §per-provider mapping → Task 2 (Ollama `think` + capability) & Task 3 (Google `thinkingBudget`); §surface (`/think`, `TWOB_THINK`, `Session.think`) → Task 4; §wiring (stream_with_retry kwarg, call sites, uniform signatures, `send` untouched) → Tasks 1 & 4; §deferred providers (accept-ignore + `supports_reasoning`→False) → Task 1; §testing → each task; §out-of-scope (display) → not built; §docs → Task 5.
- **Type consistency:** `reasoning: str | None` end-to-end; `supports_reasoning(model)->bool` on all four providers; `_think_value(model, reasoning)-> bool|str|None`; `_thinking_budget(model, reasoning)-> int|None`; `_payload(conversation, tools, thinking_budget=None)`; `_reasoning_effective(session)->str|None`; `Session.think: str|None`.
- **No broken intermediate:** Task 1 makes every provider accept the kwarg and every test fake absorb it (`**_kwargs`) before any behavior lands, so the suite is green after each task.
- **Ripple accounted:** the 12 fake-provider test files are listed explicitly in Task 1.
