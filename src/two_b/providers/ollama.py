"""Ollama adapter — native /api/chat, NEVER the /v1 OpenAI-compat shim.

Handles both local Ollama (default, no key) and Ollama Cloud (ollama.com with an
API key). This is the load-bearing rule of the whole project: local models stay
on Ollama's own protocol, because the /v1 translation measurably degrades their
tool selection. The serialized payload here is byte-equivalent to the validated
prototype's.
"""
import json
import os

import json as _json
from typing import Callable

from ..conversation import Conversation, Message, Role, ToolCall
from ..toolspec import ToolSpec, to_openai
from .base import Provider, ProviderResponse, get_json, post_json, post_stream

LOCAL_DEFAULT = "http://localhost:11434"
CLOUD_HOST = "https://ollama.com"


class OllamaProvider:
    def __init__(self, name: str = "ollama", host: str | None = None, api_key: str | None = None):
        self.name = name
        self.host = (host or os.environ.get("OLLAMA_API_BASE")
                     or os.environ.get("OLLAMA_HOST") or LOCAL_DEFAULT).rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def is_available(self) -> bool:
        if self.api_key is not None and not self.api_key:
            return False
        try:
            self.list_models()
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        data = get_json(f"{self.host}/api/tags", headers=self._headers(), provider=self.name)
        return [m["name"] for m in data.get("models", [])]

    def _messages(self, conv: Conversation) -> list[dict]:
        out = [{"role": "system", "content": conv.system_prompt}]
        for m in conv.messages:
            if m.tool_results:
                for r in m.tool_results:
                    out.append({"role": "tool", "content": r.content})
                continue
            if m.role == Role.ASSISTANT:
                entry: dict = {"role": "assistant", "content": m.text or ""}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"function": {"name": tc.name, "arguments": tc.arguments}} for tc in m.tool_calls
                    ]
                out.append(entry)
            else:
                out.append({"role": "user", "content": m.text or ""})
        return out

    def send(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...]) -> ProviderResponse:
        payload = {
            "model": model,
            "messages": self._messages(conversation),
            "tools": to_openai(tools),
            "stream": False,
        }
        raw = post_json(f"{self.host}/api/chat", payload, headers=self._headers(),
                        provider=self.name)
        msg = raw.get("message", {})
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c["function"]
            args = fn["arguments"]
            if isinstance(args, str):
                args = json.loads(args)
            calls.append(ToolCall.new(name=fn["name"], arguments=args))
        text = (msg.get("content") or "").strip()
        thinking = (msg.get("thinking") or "").strip()
        return ProviderResponse(
            message=Message.assistant(text=text or None, thinking=thinking or None, tool_calls=calls),
            raw=raw,
        )

    def stream(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...],
               on_text: Callable[[str], None]) -> ProviderResponse:
        payload = {
            "model": model,
            "messages": self._messages(conversation),
            "tools": to_openai(tools),
            "stream": True,
        }
        content, thinking, calls = [], [], []
        for line in post_stream(f"{self.host}/api/chat", payload, headers=self._headers(),
                                provider=self.name):
            line = line.strip()
            if not line:
                continue
            obj = _json.loads(line)
            m = obj.get("message", {})
            if m.get("content"):
                content.append(m["content"])
                on_text(m["content"])
            if m.get("thinking"):
                thinking.append(m["thinking"])
            for c in m.get("tool_calls") or []:
                fn = c["function"]
                args = fn["arguments"]
                if isinstance(args, str):
                    args = _json.loads(args)
                calls.append(ToolCall.new(name=fn["name"], arguments=args))
            if obj.get("done"):
                break
        text = "".join(content).strip()
        think = "".join(thinking).strip()
        return ProviderResponse(
            message=Message.assistant(text=text or None, thinking=think or None, tool_calls=calls),
            raw={},
        )


    def perf(self, model: str) -> str:
        """Live footprint of a loaded local model via /api/ps: total RAM and the
        GPU/CPU split — the local-model equivalent of a token counter. '' if the
        model isn't loaded yet or ps is unavailable."""
        try:
            data = get_json(f"{self.host}/api/ps", headers=self._headers(), provider=self.name)
        except Exception:
            return ""
        for m in data.get("models", []):
            if model in (m.get("name"), m.get("model")):
                total = m.get("size", 0) or 0
                vram = m.get("size_vram", 0) or 0
                if total <= 0:
                    return ""
                gb = total / 1e9
                if vram >= total:
                    proc = "100% GPU"
                elif vram <= 0:
                    proc = "100% CPU"
                else:
                    gpu = round(vram / total * 100)
                    proc = f"{gpu}% GPU / {100 - gpu}% CPU"
                return f"{gb:.1f}GB · {proc}"
        return ""


def local() -> OllamaProvider:
    return OllamaProvider(name="ollama")


def cloud() -> OllamaProvider | None:
    key = os.environ.get("OLLAMA_API_KEY")
    if not key:
        return None
    return OllamaProvider(name="ollama-cloud", host=CLOUD_HOST, api_key=key)
