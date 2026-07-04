"""Anthropic Messages API adapter.

Native wire format: top-level `system`, tools with `input_schema`, and
tool_use / tool_result content blocks. Foreign `thinking` (e.g. from an Ollama
model earlier in the conversation) is dropped on serialize — you can't fabricate
Anthropic's signed thinking blocks, and every provider ignores foreign reasoning
traces anyway.
"""
import os
from typing import Callable

from .. import catalog
from ..conversation import Conversation, Message, Role, ToolCall
from ..toolspec import ToolSpec, to_anthropic
from .base import ProviderResponse, post_json

API_URL = "https://api.anthropic.com/v1/messages"
_MODELS = ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-fable-5"]


class AnthropicProvider:
    name = "anthropic"

    @property
    def api_key(self) -> str:
        return os.environ.get("ANTHROPIC_API_KEY", "")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def list_models(self) -> list[str]:
        return list(_MODELS)

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    def _messages(self, conv: Conversation) -> list[dict]:
        out = []
        for m in conv.messages:
            if m.tool_results:
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r.tool_call_id,
                     "content": r.content, "is_error": r.is_error}
                    for r in m.tool_results
                ]})
                continue
            if m.role == Role.ASSISTANT:
                blocks = []
                if m.text:
                    blocks.append({"type": "text", "text": m.text})
                for tc in m.tool_calls:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            else:
                out.append({"role": "user", "content": m.text or ""})
        return out

    def send(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...]) -> ProviderResponse:
        # Prompt caching (GA — no beta header needed): mark the stable prefix
        # (system prompt, last tool definition) with cache_control so repeated
        # requests reuse Anthropic's cache instead of paying full price every
        # turn. OpenAI-compatible providers cache automatically server-side —
        # no payload change needed there.
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
        }
        raw = post_json(API_URL, payload, headers=self._headers(), provider=self.name)
        text_parts, calls = [], []
        for block in raw.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall.new(name=block.get("name", ""),
                                          arguments=block.get("input", {}), id=block.get("id")))
        text = "".join(text_parts).strip()
        return ProviderResponse(
            message=Message.assistant(text=text or None, tool_calls=calls),
            raw=raw,
        )

    def stream(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...],
               on_text: Callable[[str], None]) -> ProviderResponse:
        # Non-streaming fallback (SSE parsing not yet validated for this provider):
        # send once, then emit the full text through the same delta path so the
        # UI treats it uniformly.
        resp = self.send(conversation, model, tools)
        if resp.message.text:
            on_text(resp.message.text)
        return resp
