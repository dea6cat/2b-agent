"""Provider protocol, shared response/error types, and a stdlib HTTP helper.

Every adapter implements Provider with canonical types in and out; the wire
format is 100% private to each implementation. Uses urllib (stdlib) throughout
to keep 2B dependency-light and self-contained — no per-provider SDKs.
"""
from __future__ import annotations

import json
import time as _time
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .. import __version__
from ..conversation import Conversation, Message
from ..toolspec import ToolSpec

# Identify ourselves. urllib's default "Python-urllib/x" User-Agent is blocked by some
# providers' bot filters (e.g. Cerebras's Cloudflare returns 403 "error code: 1010"), so
# every request carries a real UA instead.
_USER_AGENT = f"2b-agent/{__version__}"


@dataclass(slots=True)
class ProviderResponse:
    message: Message
    raw: dict  # untouched provider payload, for debugging only
    # Why generation stopped, when the provider reports it (Ollama: "stop"/"length"/
    # "load"…). None when unknown. Lets the loop tell a truncated turn apart from a
    # genuine empty answer instead of re-prompting the same wall.
    done_reason: str | None = None
    # Prompt (input) tokens the provider actually counted for this request, when it
    # reports them (Ollama: prompt_eval_count). None when unknown. Used to calibrate the
    # char→token estimate the context meter and compaction trigger run on.
    prompt_tokens: int | None = None


class ProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, *, retryable: bool = False):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable


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


class Provider(Protocol):
    name: str

    def is_available(self) -> bool:
        """Cheap local check — is this provider configured/reachable enough to
        offer via /model? Must not require more than a quick call."""
        ...

    def list_models(self) -> list[str]:
        ...

    def send(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...]) -> ProviderResponse:
        ...


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


def stream_with_retry(provider, conversation, model, tools, on_text, *, retries=3, cancel=None):
    """provider.stream with backoff on retryable ProviderError (429/5xx/conn). Honors a
    cancel Event (re-raises immediately) and re-raises the last error after `retries`.
    When a retryable error survives every retry, the surfaced message gets a
    `— retried N×, still failing` suffix so the user sees it wasn't a single blip."""
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


def get_json(url: str, headers: dict | None = None, timeout: int = 15, provider: str = "http") -> dict:
    hdrs = {"User-Agent": _USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise ProviderError(provider, f"HTTP {e.code}: {e.reason}", retryable=e.code >= 500) from e
    except urllib.error.URLError as e:
        raise ProviderError(provider, f"connection failed: {e.reason}", retryable=True) from e
