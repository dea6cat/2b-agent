# ESC Global Abort Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a single ESC press an immediate global abort — every task's model call, every subprocess, every helper — for both cloud and local models, in any state.

**Architecture:** ESC can't wake a thread blocked in a C-level socket read by setting a flag. So we make the HTTP layer register its live response objects and add `abort_all_connections()`, which closes them all — any parked network read then raises at once. Every provider's streaming call threads a `cancel` Event; the Anthropic adapter switches from a blocking whole-response call to real SSE so it shares that path. A central `abort_all(session)` sets the cancel flag on *all* active and backgrounded tasks and closes every connection; the TUI's ESC handler calls it.

**Tech Stack:** Python 3, stdlib only (`urllib`, `threading`), `unittest` for tests. No new dependencies.

## Global Constraints

- **Stdlib only** — no new third-party dependencies (2B is deliberately dependency-light; `base.py` uses `urllib` throughout).
- **Tests use `unittest`**, run as `python -m unittest tests.test_<name>` from the repo root, with `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))` at the top. Mirror the existing `tests/test_esc_kill.py` style.
- **Cancellation is never an error.** A user-initiated abort must finish a task via `_finish_stopped` (quiet "Stopped.", state `ERROR`/`error="stopped"`), never `_finish_failed` (red error).
- **`_Cancelled` is non-retryable** — it must never be retried by `stream_with_retry` and must never be wrapped in a `ProviderError`.
- **Preserve existing behavior:** prompt-cache `cache_control` headers on Anthropic requests; the double-Ctrl-C quit semantics; the history-search ESC special case.
- Commit after each task. Do **not** add a `Co-Authored-By` trailer; commit as the repo user. Keep commit messages neutral (no comparison/competitor names).

---

### Task 1: Abortable HTTP layer in `base.py`

Add a live-connection registry, a `_Cancelled` sentinel, `abort_all_connections()`, and a `cancel` parameter on both HTTP helpers so a set flag short-circuits before connecting and a closed socket unwinds cleanly.

**Files:**
- Modify: `src/two_b/providers/base.py` (top-of-module additions; `post_json` ~62-81; `post_stream` ~84-106)
- Test: `tests/test_abort_connections.py` (create)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `class _Cancelled(Exception)` — raised by the HTTP helpers when their `cancel` Event is set.
  - `def abort_all_connections() -> None` — closes every live response.
  - `post_json(url, payload, headers=None, timeout=600, provider="http", cancel=None) -> dict`
  - `post_stream(url, payload, headers=None, timeout=600, provider="http", cancel=None)` — generator yielding decoded lines.

- [ ] **Step 1: Write the failing test**

Create `tests/test_abort_connections.py`:

```python
"""Tests for the abortable HTTP layer: a set cancel flag short-circuits before
connecting, abort_all_connections() closes live responses, and a socket closed
mid-read surfaces as _Cancelled (not a retryable ProviderError).

Run: `python -m unittest tests.test_abort_connections` from the repo root.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers import base  # noqa: E402
from two_b.providers.base import _Cancelled, ProviderError  # noqa: E402


class FakeResp:
    """Stand-in for a urllib response: iterable of byte lines, closeable, and it
    raises ValueError on read once closed (matching a closed socket)."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.closed = False

    def __iter__(self):
        for ln in self._lines:
            if self.closed:
                raise ValueError("read of closed file")
            yield ln

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self):
        if self.closed:
            raise ValueError("read of closed file")
        return b"".join(self._lines)

    def close(self):
        self.closed = True


class AbortLayer(unittest.TestCase):
    def tearDown(self):
        base.abort_all_connections()  # never leak a registered fake between tests

    def test_preset_cancel_short_circuits_post_stream(self):
        cancel = threading.Event()
        cancel.set()
        gen = base.post_stream("http://x", {}, cancel=cancel)
        with self.assertRaises(_Cancelled):
            next(gen)

    def test_preset_cancel_short_circuits_post_json(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(_Cancelled):
            base.post_json("http://x", {}, cancel=cancel)

    def test_abort_closes_registered_response(self):
        resp = FakeResp([b"a\n", b"b\n"])
        base._register(resp)
        base.abort_all_connections()
        self.assertTrue(resp.closed)

    def test_close_mid_stream_with_cancel_set_raises_cancelled(self):
        cancel = threading.Event()
        resp = FakeResp([b"one\n", b"two\n", b"three\n"])

        def fake_urlopen(req, timeout=600):
            return resp

        real = base.urllib.request.urlopen
        base.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, base.urllib.request, "urlopen", real)

        gen = base.post_stream("http://x", {}, cancel=cancel)
        self.assertEqual(next(gen), "one\n")     # first line arrives normally
        cancel.set()
        base.abort_all_connections()             # close the socket out from under it
        with self.assertRaises(_Cancelled):
            next(gen)

    def test_close_mid_stream_without_cancel_is_a_provider_error(self):
        # A drop we did NOT initiate must stay a retryable ProviderError, not _Cancelled.
        resp = FakeResp([b"one\n"])

        def fake_urlopen(req, timeout=600):
            return resp

        real = base.urllib.request.urlopen
        base.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, base.urllib.request, "urlopen", real)

        gen = base.post_stream("http://x", {}, provider="p")
        self.assertEqual(next(gen), "one\n")
        resp.close()                              # external drop, no cancel flag
        with self.assertRaises(ProviderError):
            next(gen)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_abort_connections -v`
Expected: FAIL — `ImportError: cannot import name '_Cancelled'` (and `_register`/`abort_all_connections` missing).

- [ ] **Step 3: Add the registry, sentinel, and abort function**

In `src/two_b/providers/base.py`, add `import threading` near the other imports (after `import time as _time`), then add this block just after the `ProviderError` class (before `post_json`):

```python
# --- Abortable connections -------------------------------------------------
# ESC/panic can't wake a thread blocked in a C-level socket read by flipping a
# flag; it has to close the socket. Every live response registers here so
# abort_all_connections() can close them all at once. A close mid-read raises
# OSError/ValueError in the blocked thread, which the helpers translate to
# _Cancelled when the caller's cancel Event is set.
_active_conns: set = set()
_conns_lock = threading.Lock()


class _Cancelled(Exception):
    """Raised by the HTTP helpers when their cancel Event is set — either before
    connecting or because abort_all_connections() closed the socket mid-read. Not
    a ProviderError, so stream_with_retry re-raises it immediately (never retries)."""


def _register(resp) -> None:
    with _conns_lock:
        _active_conns.add(resp)


def _unregister(resp) -> None:
    with _conns_lock:
        _active_conns.discard(resp)


def abort_all_connections() -> None:
    """Close every live HTTP response so any thread blocked reading one raises at
    once. Called from the esc/panic path — makes cancellation immediate regardless
    of provider or how long the model would otherwise take to respond."""
    with _conns_lock:
        conns = list(_active_conns)
        _active_conns.clear()
    for resp in conns:
        try:
            resp.close()
        except Exception:
            pass
```

- [ ] **Step 4: Make `post_json` cancel-aware**

Replace the body of `post_json` (lines ~62-81) with:

```python
def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 600,
              provider: str = "http", cancel=None) -> dict:
    """POST JSON, return parsed JSON. Raises ProviderError with a useful message.
    Honors a cancel Event: raises _Cancelled if it's already set, or if the socket
    is closed mid-read by abort_all_connections()."""
    if cancel is not None and cancel.is_set():
        raise _Cancelled()
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", "User-Agent": _USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:500]
        except Exception:
            pass
        raise ProviderError(provider, f"HTTP {e.code}: {body or e.reason}", retryable=(e.code == 429 or e.code >= 500)) from e
    except urllib.error.URLError as e:
        if cancel is not None and cancel.is_set():
            raise _Cancelled() from e
        raise ProviderError(provider, f"connection failed: {e.reason}", retryable=True) from e
    _register(resp)
    try:
        with resp:
            return json.loads(resp.read())
    except (OSError, ValueError) as e:
        if cancel is not None and cancel.is_set():
            raise _Cancelled() from e
        raise ProviderError(provider, f"read failed: {e}", retryable=True) from e
    finally:
        _unregister(resp)
```

- [ ] **Step 5: Make `post_stream` cancel-aware**

Replace the body of `post_stream` (lines ~84-106) with:

```python
def post_stream(url: str, payload: dict, headers: dict | None = None, timeout: int = 600,
                provider: str = "http", cancel=None):
    """POST JSON and yield decoded response lines as they arrive (for streaming
    NDJSON / SSE). Raises ProviderError on connection/HTTP failure. Honors a cancel
    Event: raises _Cancelled if it's already set, on each line, or if the socket is
    closed mid-read by abort_all_connections()."""
    if cancel is not None and cancel.is_set():
        raise _Cancelled()
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", "User-Agent": _USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:500]
        except Exception:
            pass
        raise ProviderError(provider, f"HTTP {e.code}: {body or e.reason}", retryable=(e.code == 429 or e.code >= 500)) from e
    except urllib.error.URLError as e:
        if cancel is not None and cancel.is_set():
            raise _Cancelled() from e
        raise ProviderError(provider, f"connection failed: {e.reason}", retryable=True) from e
    _register(resp)
    try:
        with resp:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    raise _Cancelled()
                yield raw.decode("utf-8", errors="replace")
    except (OSError, ValueError) as e:
        if cancel is not None and cancel.is_set():
            raise _Cancelled() from e
        raise ProviderError(provider, f"stream read failed: {e}", retryable=True) from e
    finally:
        _unregister(resp)
```

Note: a `_Cancelled` raised by the explicit per-line check is not an `OSError`/`ValueError`, so it propagates past the `except` untouched. Good.

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m unittest tests.test_abort_connections -v`
Expected: PASS (all 5 tests).

- [ ] **Step 7: Run the existing provider/retry tests to confirm no regression**

Run: `python -m unittest tests.test_retry tests.test_google_streaming tests.test_user_agent -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/two_b/providers/base.py tests/test_abort_connections.py
git commit -m "feat: abortable HTTP layer — close live sockets on cancel"
```

---

### Task 2: Thread `cancel` through `stream_with_retry` and the streaming providers

Forward the cancel Event from `stream_with_retry` into each provider's `stream()`, and make `_Cancelled` bypass retries. Update the three already-streaming providers (Ollama, OpenAI-compatible, Google) to accept and forward `cancel`.

**Files:**
- Modify: `src/two_b/providers/base.py` — `stream_with_retry` (~109-133)
- Modify: `src/two_b/providers/ollama.py:230-231` (signature + `post_stream` call ~242)
- Modify: `src/two_b/providers/openai_compat.py:125-126` (signature + `post_stream` call ~135)
- Modify: `src/two_b/providers/google.py:97-98` (signature + `post_stream` call ~105)
- Test: `tests/test_cancel_streaming.py` (create)

**Interfaces:**
- Consumes: `_Cancelled` from Task 1.
- Produces: each provider `stream(self, conversation, model, tools, on_text, *, cancel=None)`; `stream_with_retry(..., cancel=None)` forwards `cancel` and re-raises `_Cancelled` without retry.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cancel_streaming.py`:

```python
"""stream_with_retry forwards cancel to the provider and never retries a _Cancelled.

Run: `python -m unittest tests.test_cancel_streaming` from the repo root.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers import base  # noqa: E402
from two_b.providers.base import _Cancelled, ProviderError  # noqa: E402


class ForwardsCancel(unittest.TestCase):
    def test_cancel_is_forwarded_to_provider_stream(self):
        seen = {}

        class P:
            name = "p"

            def stream(self, conv, model, tools, on_text, *, cancel=None):
                seen["cancel"] = cancel
                return "ok"

        ev = threading.Event()
        base.stream_with_retry(P(), None, "m", (), lambda c: None, cancel=ev)
        self.assertIs(seen["cancel"], ev)

    def test_cancelled_is_not_retried(self):
        calls = {"n": 0}

        class P:
            name = "p"

            def stream(self, conv, model, tools, on_text, *, cancel=None):
                calls["n"] += 1
                raise _Cancelled()

        with self.assertRaises(_Cancelled):
            base.stream_with_retry(P(), None, "m", (), lambda c: None, cancel=threading.Event())
        self.assertEqual(calls["n"], 1, "a cancelled stream must not be retried")

    def test_retryable_provider_error_still_retries(self):
        calls = {"n": 0}

        class P:
            name = "p"

            def stream(self, conv, model, tools, on_text, *, cancel=None):
                calls["n"] += 1
                raise ProviderError("p", "boom", retryable=True)

        with self.assertRaises(ProviderError):
            base.stream_with_retry(P(), None, "m", (), lambda c: None, retries=1, cancel=None)
        self.assertEqual(calls["n"], 2, "one initial try + one retry")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_cancel_streaming -v`
Expected: FAIL — `test_cancel_is_forwarded_to_provider_stream` fails because `stream_with_retry` doesn't pass `cancel` to `provider.stream`; `test_cancelled_is_not_retried` fails because `_Cancelled` isn't caught (currently raises `TypeError` on the unexpected `cancel` kwarg, or is retried).

- [ ] **Step 3: Update `stream_with_retry`**

In `src/two_b/providers/base.py`, replace the `for attempt` loop head of `stream_with_retry` (the `try`/`except ProviderError` block, lines ~115-127) so it forwards `cancel` and treats `_Cancelled` as terminal:

```python
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            return provider.stream(conversation, model, tools, on_text, cancel=cancel)
        except _Cancelled:
            raise                                        # user aborted — never retry
        except ProviderError as e:
            cancelled = cancel is not None and cancel.is_set()
            if not e.retryable or cancelled:
                raise
            if attempt == retries:                       # exhausted every retry
                if attempt >= 1:
                    raw = str(e).removeprefix(f"[{e.provider}] ")
                    raise ProviderError(e.provider, f"{raw} — retried {attempt}×, still failing",
                                        retryable=True) from e
                raise
            waited = 0.0
            while waited < delay:
                if cancel is not None and cancel.is_set():
                    raise
                _time.sleep(0.1); waited += 0.1
            delay = min(delay * 2, 8.0)
```

(Only the `return provider.stream(...)` line changed — adding `cancel=cancel` — plus the new `except _Cancelled: raise` clause immediately after the `try`.)

- [ ] **Step 4: Update the Ollama provider**

In `src/two_b/providers/ollama.py`, change the `stream` signature (line 230-231) and the `post_stream` call (~242):

```python
    def stream(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...],
               on_text: Callable[[str], None], *, cancel=None) -> ProviderResponse:
```

```python
        for line in post_stream(f"{self.host}/api/chat", payload, headers=self._headers(),
                                provider=self.name, cancel=cancel):
```

- [ ] **Step 5: Update the OpenAI-compatible provider**

In `src/two_b/providers/openai_compat.py`, change the `stream` signature (line 125-126) and the `post_stream` call (~135):

```python
    def stream(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...],
               on_text: Callable[[str], None], *, cancel=None) -> ProviderResponse:
```

```python
        for line in post_stream(f"{self.base_url}/chat/completions", payload,
                                headers=self._headers(), provider=self.name, cancel=cancel):
```

- [ ] **Step 6: Update the Google provider**

In `src/two_b/providers/google.py`, change the `stream` signature (line 97-98) and the `post_stream` call (~105):

```python
    def stream(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...],
               on_text: Callable[[str], None], *, cancel=None) -> ProviderResponse:
```

```python
        for line in post_stream(url, self._payload(conversation, tools), headers=self._headers(),
                                provider=self.name, cancel=cancel):
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m unittest tests.test_cancel_streaming tests.test_google_streaming tests.test_retry -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/two_b/providers/base.py src/two_b/providers/ollama.py src/two_b/providers/openai_compat.py src/two_b/providers/google.py tests/test_cancel_streaming.py
git commit -m "feat: forward cancel into provider streams; never retry a cancel"
```

---

### Task 3: Anthropic real SSE streaming

Replace the Anthropic non-streaming fallback `stream()` with a real SSE parser via `post_stream`, threading `cancel`. This removes the blocking whole-response wait, shares the abortable path, and streams tokens live. Preserve prompt-cache headers.

**Files:**
- Modify: `src/two_b/providers/anthropic.py` — imports (9-15), `stream` (89-97)
- Test: `tests/test_anthropic_streaming.py` (create)

**Interfaces:**
- Consumes: `post_stream` (Task 1), `_Cancelled` (Task 1).
- Produces: `AnthropicProvider.stream(self, conversation, model, tools, on_text, *, cancel=None) -> ProviderResponse` that emits text deltas via `on_text`, assembles `tool_use` calls, and sets `done_reason`/`prompt_tokens`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_anthropic_streaming.py`:

```python
"""AnthropicProvider.stream parses the Messages SSE stream: text deltas flow to
on_text, tool_use blocks assemble from input_json_delta fragments, and usage/stop
are captured. cancel is honored between events.

Run: `python -m unittest tests.test_anthropic_streaming` from the repo root.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers import anthropic as anth  # noqa: E402
from two_b.providers.base import _Cancelled  # noqa: E402

# A minimal but representative Messages SSE stream: one text block, then one
# tool_use block whose JSON arrives in two fragments.
_SSE = [
    'event: message_start\n',
    'data: {"type":"message_start","message":{"usage":{"input_tokens":42}}}\n',
    '\n',
    'event: content_block_start\n',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n',
    '\n',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}\n',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}\n',
    'data: {"type":"content_block_stop","index":0}\n',
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu_1","name":"read_file"}}\n',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}\n',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"a.py\\"}"}}\n',
    'data: {"type":"content_block_stop","index":1}\n',
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}\n',
    'data: {"type":"message_stop"}\n',
]


class Streaming(unittest.TestCase):
    def _patch_stream(self, lines):
        real = anth.post_stream
        anth.post_stream = lambda *a, **k: iter(lines)
        self.addCleanup(setattr, anth, "post_stream", real)

    def test_text_and_tool_use_are_parsed(self):
        self._patch_stream(_SSE)
        chunks = []
        conv = _FakeConv()
        resp = anth.AnthropicProvider().stream(conv, "claude-opus-4-8", (), chunks.append)
        self.assertEqual("".join(chunks), "Hello")
        self.assertEqual(resp.message.text, "Hello")
        self.assertEqual(len(resp.message.tool_calls), 1)
        tc = resp.message.tool_calls[0]
        self.assertEqual(tc.name, "read_file")
        self.assertEqual(tc.id, "tu_1")
        self.assertEqual(tc.arguments, {"path": "a.py"})
        self.assertEqual(resp.prompt_tokens, 42)
        self.assertEqual(resp.done_reason, "tool_use")

    def test_cancel_between_events_raises(self):
        cancel = threading.Event()

        def gen():
            yield 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
            yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n'
            cancel.set()
            # A real post_stream would raise _Cancelled itself; emulate that here.
            raise _Cancelled()

        real = anth.post_stream
        anth.post_stream = lambda *a, **k: gen()
        self.addCleanup(setattr, anth, "post_stream", real)
        with self.assertRaises(_Cancelled):
            anth.AnthropicProvider().stream(_FakeConv(), "m", (), lambda c: None, cancel=cancel)


class _FakeConv:
    system_prompt = "sys"
    messages = []


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_anthropic_streaming -v`
Expected: FAIL — the current `stream()` calls the blocking `send()` (which would try a real HTTP request) and never consults `post_stream`, so `test_text_and_tool_use_are_parsed` fails (no parsing of the patched stream).

- [ ] **Step 3: Update imports**

In `src/two_b/providers/anthropic.py`, replace the import block (lines 9-15) with:

```python
import json
import os
from typing import Callable

from .. import catalog
from ..conversation import Conversation, Message, Role, ToolCall
from ..toolspec import ToolSpec, to_anthropic
from .base import ProviderResponse, post_json, post_stream
```

- [ ] **Step 4: Rewrite `stream()`**

In `src/two_b/providers/anthropic.py`, replace the whole `stream` method (lines 89-97) with:

```python
    def stream(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...],
               on_text: Callable[[str], None], *, cancel=None) -> ProviderResponse:
        # Real SSE: emit text deltas as they arrive and assemble tool_use blocks
        # from input_json_delta fragments. Sharing post_stream means esc closes the
        # socket and aborts immediately, same as every other provider.
        tools_json = to_anthropic(tools)
        if tools_json:
            tools_json[-1] = {**tools_json[-1], "cache_control": {"type": "ephemeral"}}
        payload = {
            "model": model,
            "max_tokens": catalog.max_tokens(model, 4096),
            "system": [{"type": "text", "text": conversation.system_prompt,
                        "cache_control": {"type": "ephemeral"}}],
            "tools": tools_json,
            "messages": self._messages(conversation),
            "stream": True,
        }
        text_parts: list[str] = []
        blocks: dict[int, dict] = {}   # index -> {"type", "name", "id", "json"}
        stop_reason = None
        prompt_tokens = None
        for line in post_stream(API_URL, payload, headers=self._headers(),
                                provider=self.name, cancel=cancel):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                evt = json.loads(data)
            except ValueError:
                continue
            etype = evt.get("type")
            if etype == "message_start":
                usage = (evt.get("message") or {}).get("usage") or {}
                prompt_tokens = usage.get("input_tokens", prompt_tokens)
            elif etype == "content_block_start":
                idx = evt.get("index", 0)
                cb = evt.get("content_block") or {}
                blocks[idx] = {"type": cb.get("type"), "name": cb.get("name", ""),
                               "id": cb.get("id"), "json": ""}
            elif etype == "content_block_delta":
                idx = evt.get("index", 0)
                delta = evt.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    chunk = delta.get("text", "")
                    if chunk:
                        text_parts.append(chunk)
                        on_text(chunk)
                elif dtype == "input_json_delta":
                    slot = blocks.setdefault(idx, {"type": "tool_use", "name": "", "id": None, "json": ""})
                    slot["json"] += delta.get("partial_json", "")
            elif etype == "message_delta":
                stop_reason = (evt.get("delta") or {}).get("stop_reason", stop_reason)
            elif etype == "message_stop":
                break
        calls = []
        for idx in sorted(blocks):
            b = blocks[idx]
            if b.get("type") != "tool_use":
                continue
            try:
                args = json.loads(b["json"]) if b["json"] else {}
            except ValueError:
                args = {}
            calls.append(ToolCall.new(name=b["name"], arguments=args, id=b["id"]))
        text = "".join(text_parts).strip()
        return ProviderResponse(
            message=Message.assistant(text=text or None, tool_calls=calls),
            raw={},
            done_reason=stop_reason,
            prompt_tokens=prompt_tokens,
        )
```

Leave `send()` unchanged — it stays available for any non-streaming caller.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_anthropic_streaming -v`
Expected: PASS (both tests).

- [ ] **Step 6: Run the prompt-cache test to confirm headers/cache_control still intact**

Run: `python -m unittest tests.test_prompt_cache -v`
Expected: PASS. If `test_prompt_cache` asserts on `send()`'s payload only, it stays green; if it also checks `stream()`, confirm the `cache_control` markers above still satisfy it.

- [ ] **Step 7: Commit**

```bash
git add src/two_b/providers/anthropic.py tests/test_anthropic_streaming.py
git commit -m "feat: stream the Anthropic provider over SSE (abortable, live tokens)"
```

---

### Task 4: Cancel-aware compaction + orchestrator treats `_Cancelled` as a stop

Thread the task's cancel flag into the summarizer call, and make `run_task` finish quietly (`_finish_stopped`) when a `_Cancelled` escapes either streaming call — never as a failure.

**Files:**
- Modify: `src/two_b/orchestrator.py` — import (~40), `compact_conversation` signature (639) + summarizer call (676), `_maybe_compact` call to `compact_conversation` (~720), and the two `stream_with_retry` try/except blocks (~1499-1506 and ~1692 onward)
- Test: `tests/test_cancel_orchestration.py` (create)

**Interfaces:**
- Consumes: `_Cancelled` (Task 1), cancel-aware `provider.stream` (Tasks 2-3).
- Produces: `compact_conversation(conv, provider, model, touched=None, breadcrumb="", cancel=None)`; `run_task` maps `_Cancelled` to `_finish_stopped`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cancel_orchestration.py`:

```python
"""A _Cancelled escaping the model stream finishes the task as 'stopped', not
'failed', and compaction forwards the task's cancel flag.

Run: `python -m unittest tests.test_cancel_orchestration` from the repo root.
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402


class CancelMapping(unittest.TestCase):
    def test_run_task_source_maps_cancelled_to_stopped(self):
        # Guard: both stream paths must catch _Cancelled and route to _finish_stopped.
        # (Written as `except (_Interrupted, _Cancelled)`, so assert on the name, not the
        # exact clause text, and require it in both stream paths.)
        src = inspect.getsource(orchestrator.run_task)
        self.assertGreaterEqual(src.count("_Cancelled"), 2, "both stream paths must catch _Cancelled")
        # And _Cancelled must be imported so the except can reference it.
        self.assertTrue(hasattr(orchestrator, "_Cancelled"))

    def test_compact_conversation_accepts_cancel(self):
        sig = inspect.signature(orchestrator.compact_conversation)
        self.assertIn("cancel", sig.parameters)


if __name__ == "__main__":
    unittest.main()
```

(Note: `run_task` drives a full session and is impractical to exercise end-to-end in a unit test without a fake provider/session harness; this task guards the two structural invariants that make cancellation behave. The end-to-end behavior is verified manually in the final verification step.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_cancel_orchestration -v`
Expected: FAIL — `orchestrator` has no `_Cancelled` attribute and `run_task` has no `except _Cancelled`.

- [ ] **Step 3: Import `_Cancelled`**

In `src/two_b/orchestrator.py`, update the base import (line 40) to:

```python
from .providers.base import ProviderError, _Cancelled, stream_with_retry
```

- [ ] **Step 4: Add `cancel` to `compact_conversation` and forward it**

In `src/two_b/orchestrator.py`, change the `compact_conversation` signature (line 639):

```python
def compact_conversation(conv: Conversation, provider, model: str, touched=None, breadcrumb: str = "", cancel=None):
```

and the summarizer stream call (line 676):

```python
    resp = provider.stream(summ, model, (), lambda c: buf.append(c), cancel=cancel)
```

- [ ] **Step 5: Pass the task's flag from `_maybe_compact`**

In `src/two_b/orchestrator.py`, update the `compact_conversation(...)` call inside `_maybe_compact` (~line 720) to forward the flag:

```python
        dropped = compact_conversation(conv, provider, model, touched=touched,
                                       breadcrumb=_ARCHIVE_BREADCRUMB if archiving else "",
                                       cancel=task.cancel_flag)
```

(`_maybe_compact` already wraps its body in a `try/except` that swallows failures, so a `_Cancelled` raised here is swallowed and compaction is simply skipped; the subsequent stream call then aborts the task.)

- [ ] **Step 6: Map `_Cancelled` to a stop in the mid-stream path**

In `src/two_b/orchestrator.py`, in the first streaming `try` block (lines ~1499-1506), add a `_Cancelled` clause alongside `_Interrupted`:

```python
            try:
                resp = stream_with_retry(provider, req_conv, model, active_specs, on_text, cancel=task.cancel_flag)
            except (_Interrupted, _Cancelled):
                _finish_stopped(task, on_event)
                return
            except Exception as e:
                _finish_failed(task, on_event, _classify_exc(e))
                return
```

- [ ] **Step 7: Map `_Cancelled` to a stop in the final-answer path**

In `src/two_b/orchestrator.py`, the final `stream_with_retry` call (~line 1692) sits in a `try` whose `except` clauses handle `_Interrupted`/errors. Add `_Cancelled` to the interrupt handling there the same way. Locate the `except _Interrupted:` clause that follows this final block and change it to `except (_Interrupted, _Cancelled):`. If that block instead only has a broad `except Exception`, add an explicit `except (_Interrupted, _Cancelled): _finish_stopped(task, on_event); return` **before** it.

Verify the edit:

Run: `grep -n "_Cancelled" src/two_b/orchestrator.py`
Expected: the import line plus two `except (_Interrupted, _Cancelled)` occurrences (one per stream path).

- [ ] **Step 8: Run test to verify it passes**

Run: `python -m unittest tests.test_cancel_orchestration -v`
Expected: PASS.

- [ ] **Step 9: Run the compaction and turn-closure tests for regression**

Run: `python -m unittest tests.test_compaction_hardening tests.test_turn_closure -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/two_b/orchestrator.py tests/test_cancel_orchestration.py
git commit -m "feat: cancel-aware compaction; treat cancelled stream as a stop"
```

---

### Task 5: Global `abort_all()` panic + ESC targets every task

Add `orchestrator.abort_all(session)` that sets the cancel flag on **all** active and backgrounded tasks and closes every live connection, then wire the TUI's ESC handler to it.

**Files:**
- Modify: `src/two_b/orchestrator.py` — add `abort_all` (near `teardown_helpers`, ~414) and re-export `abort_all_connections`
- Modify: `src/two_b/app_tui.py` — `action_interrupt` (645-666)
- Test: `tests/test_abort_all.py` (create)

**Interfaces:**
- Consumes: `abort_all_connections` (Task 1); `TaskState`, `Session`, `Task` (session module).
- Produces: `orchestrator.abort_all(session) -> int` — sets `cancel_flag` (and clears steer) on every `ACTIVE`/`BACKGROUNDED` task, closes all connections, returns the number of tasks aborted.

- [ ] **Step 1: Write the failing test**

Create `tests/test_abort_all.py`:

```python
"""orchestrator.abort_all sets the cancel flag on every active AND backgrounded
task, clears their steer, closes live connections, and returns the count.

Run: `python -m unittest tests.test_abort_all` from the repo root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402
from two_b.providers import base  # noqa: E402
from two_b.session import Session, Task, TaskState  # noqa: E402


class AbortAll(unittest.TestCase):
    def _task(self, state):
        t = Task.new("do a thing") if hasattr(Task, "new") else Task(title="do a thing")
        t.state = state
        return t

    def test_aborts_active_and_backgrounded_only(self):
        s = Session(cwd=os.getcwd()) if "cwd" in Session.__init__.__code__.co_varnames else Session()
        active = self._task(TaskState.ACTIVE)
        bg = self._task(TaskState.BACKGROUNDED)
        done = self._task(TaskState.DONE)
        s.tasks = [active, bg, done]

        closed = {"n": 0}
        real = base.abort_all_connections
        base.abort_all_connections = lambda: closed.__setitem__("n", closed["n"] + 1)
        self.addCleanup(setattr, base, "abort_all_connections", real)

        n = orchestrator.abort_all(s)

        self.assertEqual(n, 2)
        self.assertTrue(active.cancel_flag.is_set())
        self.assertTrue(bg.cancel_flag.is_set())
        self.assertFalse(done.cancel_flag.is_set())
        self.assertEqual(closed["n"], 1, "must close live connections exactly once")


if __name__ == "__main__":
    unittest.main()
```

Note: adjust the `Task`/`Session` construction in `_task`/`test_...` to match the real constructors — read `src/two_b/session.py` for the exact factory (e.g. `Task.new(...)`) and required `Session` fields, and use them directly rather than the `hasattr` guard if the API is clear.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_abort_all -v`
Expected: FAIL — `orchestrator` has no `abort_all`.

- [ ] **Step 3: Add `abort_all` and re-export the connection closer**

In `src/two_b/orchestrator.py`, near `teardown_helpers` (~line 414), add — and add the re-export so callers have one import surface:

```python
from .providers.base import abort_all_connections  # re-exported for the esc/panic path


def abort_all(session: Session) -> int:
    """Global panic: set the cancel flag on every running task (foreground AND
    backgrounded), clear any pending steer, and close all live HTTP connections so
    parked model calls abort at once. Subprocess tools then die within ~100ms via
    their own cancel poll. Returns how many tasks were aborted."""
    tasks = [t for t in session.tasks
             if t.state in (TaskState.ACTIVE, TaskState.BACKGROUNDED)]
    for t in tasks:
        t.clear_steer()
        t.cancel_flag.set()
    abort_all_connections()
    return len(tasks)
```

(Place the `from .providers.base import abort_all_connections` line with the other imports at the top of the file if the linter prefers; inline here only for locality of explanation. `Session` and `TaskState` are already imported at line 41.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_abort_all -v`
Expected: PASS.

- [ ] **Step 5: Rewrite `action_interrupt` to use the panic**

In `src/two_b/app_tui.py`, replace the body of `action_interrupt` (lines 645-666) with:

```python
    def action_interrupt(self, announce: bool = True) -> bool:
        """Esc handler — a global panic button. Aborts every running task (foreground
        and backgrounded): sets each cancel flag, closes all live model connections so
        generation stops immediately, and tears down the LSP/MCP helpers off-thread.
        Returns True only if it actually aborted at least one task (so the caller — e.g.
        double-Ctrl-C — can report honestly). If a scrollback search is open, esc exits
        that instead and returns False. `announce=False` suppresses the 'stopping…' line."""
        if self._history is not None:
            self._exit_history_search()
            return False
        # P11: snap scrollback to the bottom first, so the "stopping…" line is in view.
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)
        n = orchestrator.abort_all(self.session)
        if n == 0:
            return False
        # Tear down the long-lived helpers (LSP/MCP) off the UI thread so a slow
        # server can't freeze the interface while we stop everything.
        threading.Thread(target=orchestrator.teardown_helpers, daemon=True).start()
        if announce:
            msg = "stopping…" if n == 1 else f"stopping everything ({n} tasks)…"
            self.log_write(Text(msg, style=self.c("faint")))
        return True
```

- [ ] **Step 6: Verify the TUI module imports cleanly**

Run: `python -c "import sys; sys.path.insert(0, 'src'); import two_b.app_tui"`
Expected: no output, exit 0 (no import/syntax error).

- [ ] **Step 7: Run the full test suite**

Run: `python -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -20`
Expected: `OK` (all tests pass).

- [ ] **Step 8: Commit**

```bash
git add src/two_b/orchestrator.py src/two_b/app_tui.py tests/test_abort_all.py
git commit -m "feat: ESC is a global panic button — abort every running task at once"
```

---

## Manual verification (after all tasks)

The unit tests cover the mechanics; verify the end-to-end feel in the real TUI:

1. Start 2B against an **Anthropic** model. Send a prompt that produces a long answer. Press ESC mid-generation — output must stop within a beat and show "stopping…", not run to completion.
2. Start a long `run_command` (e.g. ask it to run `sleep 30`). Press ESC — the command dies within ~100 ms (already covered by `test_esc_kill`, confirm it still holds).
3. Start a task, Ctrl-B to background it while it's generating, start a second task, then press ESC — **both** stop, and the message reads "stopping everything (2 tasks)…".
4. Repeat step 1 against a **local Ollama** model — ESC is equally immediate.

## Self-review notes

- **Spec coverage:** abortable HTTP layer + `abort_all_connections` (Task 1) → spec §1; Anthropic SSE (Task 3) → spec §2; thread cancel everywhere incl. compaction (Tasks 2, 4) → spec §3; centralized `abort_all` + ESC targeting all tasks (Task 5) → spec §4; `_Cancelled` non-retryable + cancelled≠error mapping (Tasks 2, 4) → spec "Error handling"; test list → spec "Testing".
- **TCP-connect window** (spec "known bounded window") is intentionally not engineered away; no task attempts it, matching the spec's decision.
- **Type consistency:** `stream(self, conversation, model, tools, on_text, *, cancel=None)` is identical across all four providers and matches the `stream_with_retry` call site; `abort_all_connections()`, `abort_all(session)->int`, `_Cancelled`, `compact_conversation(..., cancel=None)` names are used consistently across tasks.
