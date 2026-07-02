"""Provider protocol, shared response/error types, and a stdlib HTTP helper.

Every adapter implements Provider with canonical types in and out; the wire
format is 100% private to each implementation. Uses urllib (stdlib) throughout
to keep 2B dependency-light and self-contained — no per-provider SDKs.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from ..conversation import Conversation, Message
from ..toolspec import ToolSpec


@dataclass(slots=True)
class ProviderResponse:
    message: Message
    raw: dict  # untouched provider payload, for debugging only


class ProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, *, retryable: bool = False):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable


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
              provider: str = "http") -> dict:
    """POST JSON, return parsed JSON. Raises ProviderError with a useful message."""
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:500]
        except Exception:
            pass
        raise ProviderError(provider, f"HTTP {e.code}: {body or e.reason}", retryable=e.code >= 500) from e
    except urllib.error.URLError as e:
        raise ProviderError(provider, f"connection failed: {e.reason}", retryable=True) from e


def post_stream(url: str, payload: dict, headers: dict | None = None, timeout: int = 600,
                provider: str = "http"):
    """POST JSON and yield decoded response lines as they arrive (for streaming
    NDJSON / SSE). Raises ProviderError on connection/HTTP failure."""
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
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
        raise ProviderError(provider, f"HTTP {e.code}: {body or e.reason}", retryable=e.code >= 500) from e
    except urllib.error.URLError as e:
        raise ProviderError(provider, f"connection failed: {e.reason}", retryable=True) from e
    with resp:
        for raw in resp:
            yield raw.decode("utf-8", errors="replace")


def get_json(url: str, headers: dict | None = None, timeout: int = 15, provider: str = "http") -> dict:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise ProviderError(provider, f"HTTP {e.code}: {e.reason}", retryable=e.code >= 500) from e
    except urllib.error.URLError as e:
        raise ProviderError(provider, f"connection failed: {e.reason}", retryable=True) from e
