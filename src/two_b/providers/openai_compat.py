"""One adapter for every OpenAI-compatible Chat Completions service.

OpenAI, OpenRouter, Mistral, NVIDIA (and others) all speak the same
/v1/chat/completions wire format natively — so using it here is not a shim, it's
each service's own protocol. (Contrast Ollama LOCAL, whose native protocol is
/api/chat, handled separately — we never route local Ollama through /v1.)

Configured per instance with base_url + API-key env var + a model list. New
services are added as data in registry.py, not new code.
"""
import json
import os
from typing import Callable

from ..conversation import Conversation, Message, Role, ToolCall
from ..toolspec import ToolSpec, to_openai
from .base import ProviderResponse, get_json, post_json, post_stream


class OpenAICompatProvider:
    def __init__(self, name: str, base_url: str, key_env: str,
                 models: list[str] | None = None, dynamic_models: bool = False,
                 extra_headers: dict | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.key_env = key_env
        self._static_models = models or []
        self._dynamic = dynamic_models
        self._extra_headers = extra_headers or {}

    @property
    def api_key(self) -> str:
        return os.environ.get(self.key_env, "")

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.api_key}"}
        h.update(self._extra_headers)
        return h

    def is_available(self) -> bool:
        return bool(self.api_key)

    def list_models(self) -> list[str]:
        if not self._dynamic:
            return list(self._static_models)
        try:
            data = get_json(f"{self.base_url}/models", headers=self._headers(), provider=self.name)
            ids = [m.get("id", "") for m in data.get("data", [])]
            return [i for i in ids if i] or list(self._static_models)
        except Exception:
            return list(self._static_models)

    def _messages(self, conv: Conversation) -> list[dict]:
        out = [{"role": "system", "content": conv.system_prompt}]
        for m in conv.messages:
            if m.tool_results:
                for r in m.tool_results:
                    out.append({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content})
                continue
            if m.role == Role.ASSISTANT:
                entry: dict = {"role": "assistant", "content": m.text or ""}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                        for tc in m.tool_calls
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
        raw = post_json(f"{self.base_url}/chat/completions", payload,
                        headers=self._headers(), provider=self.name)
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            args = fn.get("arguments") or "{}"
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append(ToolCall.new(name=fn.get("name", ""), arguments=args, id=c.get("id")))
        text = (msg.get("content") or "").strip()
        return ProviderResponse(
            message=Message.assistant(text=text or None, tool_calls=calls),
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
        content = []
        by_index: dict[int, dict] = {}   # assemble tool_calls from streamed fragments
        for line in post_stream(f"{self.base_url}/chat/completions", payload,
                                headers=self._headers(), provider=self.name):
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or [{}]
            delta = choices[0].get("delta", {})
            if delta.get("content"):
                content.append(delta["content"])
                on_text(delta["content"])
            for frag in delta.get("tool_calls") or []:
                idx = frag.get("index", 0)
                slot = by_index.setdefault(idx, {"id": None, "name": "", "args": ""})
                if frag.get("id"):
                    slot["id"] = frag["id"]
                fn = frag.get("function", {})
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
        calls = []
        for _, slot in sorted(by_index.items()):
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall.new(name=slot["name"], arguments=args, id=slot["id"]))
        text = "".join(content).strip()
        return ProviderResponse(
            message=Message.assistant(text=text or None, tool_calls=calls),
            raw={},
        )
