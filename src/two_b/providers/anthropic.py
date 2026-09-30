"""Anthropic Messages API adapter.

Native wire format: top-level `system`, tools with `input_schema`, and
tool_use / tool_result content blocks. Foreign `thinking` (e.g. from an Ollama
model earlier in the conversation) is dropped on serialize — you can't fabricate
Anthropic's signed thinking blocks, and every provider ignores foreign reasoning
traces anyway.
"""
import json
import os
from typing import Callable

from . import catalog
from ..core.conversation import Conversation, Message, Role, ToolCall
from ..tooling.toolspec import ToolSpec, to_anthropic
from .base import ProviderResponse, cached_model_list, get_json, post_json, post_stream

API_URL = "https://api.anthropic.com/v1/messages"
_DEFAULT_MAX_TOKENS = 64000   # streaming output cap when the catalog doesn't know the model

# Thinking controls differ by family (prefix-matched; always-on is checked first because
# "claude-opus-5" is also a prefix of "claude-opus-5-5"). Always-on models reject a disabled
# thinking config or a budget — effort is their only dial. Adaptive models take adaptive or
# disabled. Older models use a fixed thinking budget; 2B doesn't enable that path.
_ALWAYS_ON = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5", "claude-mythos-5")
_ADAPTIVE = ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
             "claude-sonnet-5", "claude-sonnet-4-6")
_SUMMARIZED_BY_DEFAULT = ("claude-opus-4-6", "claude-sonnet-4-6")   # newer ones default to "omitted"
_EFFORTS = ("low", "medium", "high")


class AnthropicProvider:
    name = "anthropic"

    @property
    def api_key(self) -> str:
        return os.environ.get("ANTHROPIC_API_KEY", "")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def list_models(self) -> list[str]:
        return cached_model_list(self, self._fetch_models)

    def _fetch_models(self) -> list[str]:
        data = get_json("https://api.anthropic.com/v1/models?limit=1000", headers=self._headers(), provider=self.name)
        return sorted(m["id"] for m in data.get("data", []) if m.get("id"))

    def supports_reasoning(self, model: str) -> bool:
        return model.lower().startswith(_ALWAYS_ON + _ADAPTIVE)

    def _thinking_fields(self, model: str, reasoning) -> dict:
        """Request fields for a /think level: `thinking` and/or `output_config.effort`, per the
        model family. {} leaves the model's own default. Summarized thinking is requested so it
        can stream to the thinking channel; "off" (also used by compaction) never shows it."""
        m = model.lower()
        effort = {"output_config": {"effort": reasoning}} if reasoning in _EFFORTS else {}
        if m.startswith(_ALWAYS_ON):
            if reasoning == "off":            # can't disable: lowest effort, reasoning not shown
                return {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}
            return {"thinking": {"type": "adaptive", "display": "summarized"}, **effort}
        if m.startswith(_ADAPTIVE):
            if reasoning is None:
                return {}
            if reasoning == "off":
                return {"thinking": {"type": "disabled"}}
            thinking = {"type": "adaptive"} if m.startswith(_SUMMARIZED_BY_DEFAULT) \
                else {"type": "adaptive", "display": "summarized"}
            return {"thinking": thinking, **effort}
        return {}

    def _payload(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...], reasoning=None) -> dict:
        # Prompt caching (GA — no beta header needed): mark the stable prefix
        # (system prompt, last tool definition) with cache_control so repeated
        # requests reuse Anthropic's cache instead of paying full price every
        # turn. OpenAI-compatible providers cache automatically server-side —
        # no payload change needed there.
        tools_json = to_anthropic(tools)
        if tools_json:
            tools_json[-1] = {**tools_json[-1], "cache_control": {"type": "ephemeral"}}
        return {
            "model": model,
            "max_tokens": catalog.max_tokens(model, _DEFAULT_MAX_TOKENS),
            "system": [{"type": "text", "text": conversation.system_prompt,
                        "cache_control": {"type": "ephemeral"}}],
            "tools": tools_json,
            "messages": self._messages(conversation),
            **self._thinking_fields(model, reasoning),
        }

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    def _messages(self, conv: Conversation) -> list[dict]:
        # Thinking is never replayed: a replayed block is only valid while every earlier turn is
        # byte-identical, and 2B trims old tool results each request (conversation.trimmed),
        # which Anthropic counts as a history edit. A request with no replayed blocks is always
        # valid; the model just starts each turn without its earlier reasoning.
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
        payload = self._payload(conversation, model, tools)
        payload["max_tokens"] = min(payload["max_tokens"], 16000)   # non-streaming: stay under HTTP timeouts
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
               on_text: Callable[[str], None], *, cancel=None, reasoning=None, on_thinking=None) -> ProviderResponse:
        # Real SSE: emit text deltas as they arrive and assemble tool_use blocks
        # from input_json_delta fragments. Sharing post_stream means esc closes the
        # socket and aborts immediately, same as every other provider.
        payload = {**self._payload(conversation, model, tools, reasoning), "stream": True}
        text_parts: list[str] = []
        thinking_parts: list[str] = []
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
                elif dtype == "thinking_delta":
                    chunk = delta.get("thinking", "")
                    if chunk:
                        thinking_parts.append(chunk)
                        if on_thinking:
                            on_thinking(chunk)
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
        thinking = "".join(thinking_parts).strip()
        return ProviderResponse(
            message=Message.assistant(text=text or None, thinking=thinking or None, tool_calls=calls),
            raw={},
            done_reason=stop_reason,
            prompt_tokens=prompt_tokens,
        )
