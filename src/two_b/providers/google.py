"""Google Gemini adapter (generateContent).

Native wire format: `contents` with role user/model and parts; tools as
functionDeclarations; functionCall / functionResponse parts. Gemini keys a
functionResponse by the tool's NAME, not an id, so we resolve each result's
tool_call_id back to the name via the tool_calls seen earlier in the
conversation.
"""
import json
import os
from typing import Callable

from ..core.conversation import Conversation, Message, Role, ToolCall
from ..tooling.toolspec import ToolSpec, to_gemini
from .base import ProviderResponse, cached_model_list, get_json, post_json, post_stream

BASE = "https://generativelanguage.googleapis.com/v1beta"
_NON_CHAT = ("tts", "image", "transcribe", "computer-use", "robotics")   # listed, but not coding chat models
_G_LOW, _G_MED, _G_HIGH = 2048, 8192, 24576   # bounded thinking budgets; never -1 (dynamic)
# Gemini 3 requires each replayed functionCall part to carry the thoughtSignature it was
# returned with (400 otherwise). For calls Gemini didn't produce — another model's turn, or
# a session saved before signatures were kept — Google documents this placeholder.
_SKIP_SIGNATURE = "skip_thought_signature_validator"


class GoogleProvider:
    name = "google"

    @property
    def api_key(self) -> str:
        return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""

    def is_available(self) -> bool:
        return bool(self.api_key)

    def list_models(self) -> list[str]:
        return cached_model_list(self, self._fetch_models)

    def _fetch_models(self) -> list[str]:
        # Gemini chat models only: the endpoint also lists embedders, Gemma (no function
        # calling here), and special-purpose variants that can't drive a coding tool loop.
        data = get_json(f"{BASE}/models?pageSize=1000", headers=self._headers(), provider=self.name)
        names = (m.get("name", "").removeprefix("models/") for m in data.get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", []))
        return sorted(n for n in names if n.startswith("gemini-") and not any(t in n for t in _NON_CHAT))

    def _contents(self, conv: Conversation) -> list[dict]:
        # Map tool_call_id -> tool name, so tool results can name their function.
        id_to_name: dict[str, str] = {}
        for m in conv.messages:
            for tc in m.tool_calls:
                id_to_name[tc.id] = tc.name

        contents = []
        for m in conv.messages:
            if m.tool_results:
                parts = [{"functionResponse": {
                    "name": id_to_name.get(r.tool_call_id, "tool"),
                    "response": {"result": r.content},
                }} for r in m.tool_results]
                contents.append({"role": "user", "parts": parts})
                continue
            if m.role == Role.ASSISTANT:
                parts = []
                if m.text:
                    parts.append({"text": m.text})
                for tc in m.tool_calls:
                    parts.append({"functionCall": {"name": tc.name, "args": tc.arguments},
                                  "thoughtSignature": tc.signature or _SKIP_SIGNATURE})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:
                contents.append({"role": "user", "parts": [{"text": m.text or ""}]})
        return contents

    def supports_reasoning(self, model: str) -> bool:
        return model.startswith(("gemini-2.5", "gemini-3"))

    def _thinking_level(self, model: str, reasoning):
        """thinkingLevel for Gemini 3, or None for other models. Gemini 3 can't disable
        thinking and not every 3.x takes "minimal", so "off" is its lowest common level; None ->
        capped medium, matching the 2.5 budget default."""
        if not model.startswith("gemini-3"):
            return None
        return {"off": "low", "low": "low", "high": "high"}.get(reasoning, "medium")

    def _thinking_budget(self, model: str, reasoning):
        """thinkingBudget for thinkingConfig, or None to omit it (unsupported model). None ->
        capped MED (protect latency); never -1/dynamic. 2.5 Pro can't fully disable, so 'off'
        uses its minimum."""
        if not model.startswith("gemini-2.5"):
            return None
        if reasoning is None:
            return _G_MED
        tier = {"off": 0, "low": _G_LOW, "medium": _G_MED, "on": _G_MED, "high": _G_HIGH}.get(reasoning, _G_MED)
        if tier == 0 and "pro" in model:
            return 128
        return tier

    def _payload(self, conversation: Conversation, tools: tuple[ToolSpec, ...], thinking_budget=None,
                 include_thoughts=False, thinking_level=None) -> dict:
        p = {
            "systemInstruction": {"parts": [{"text": conversation.system_prompt}]},
            "contents": self._contents(conversation),
            "tools": to_gemini(tools),
        }
        if thinking_budget is not None or thinking_level is not None:
            tc = {"thinkingLevel": thinking_level} if thinking_level is not None else {"thinkingBudget": thinking_budget}
            if include_thoughts:
                tc["includeThoughts"] = True
            p["generationConfig"] = {"thinkingConfig": tc}
        return p

    # The key rides in the x-goog-api-key header (as the official SDK / Google guidance do)
    # rather than a ?key= URL param, so the secret never lands in a URL (logs, tracing, export).
    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key}

    @staticmethod
    def _read_parts(cand: dict, text_parts: list, calls: list, thought_parts: list | None = None) -> str:
        """Pull text + functionCall parts out of one candidate. Gemini marks reasoning-summary
        parts with `thought: true`; those go to thought_parts (when provided) and NEVER into the
        answer text. Returns the answer text this candidate contributed (for streaming emission)."""
        chunk_text = []
        for p in cand.get("content", {}).get("parts", []) or []:
            if "text" in p:
                if p.get("thought"):
                    if thought_parts is not None:
                        thought_parts.append(p["text"])
                else:
                    chunk_text.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                calls.append(ToolCall.new(name=fc.get("name", ""), arguments=fc.get("args", {}),
                                          signature=p.get("thoughtSignature")))
        joined = "".join(chunk_text)
        if joined:
            text_parts.append(joined)
        return joined

    def send(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...]) -> ProviderResponse:
        url = f"{BASE}/models/{model}:generateContent"
        raw = post_json(url, self._payload(conversation, tools), headers=self._headers(), provider=self.name)
        text_parts, calls = [], []
        self._read_parts((raw.get("candidates") or [{}])[0], text_parts, calls)
        text = "".join(text_parts).strip()
        return ProviderResponse(message=Message.assistant(text=text or None, tool_calls=calls), raw=raw)

    def stream(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...],
               on_text: Callable[[str], None], *, cancel=None, reasoning=None, on_thinking=None) -> ProviderResponse:
        # Gemini SSE: :streamGenerateContent?alt=sse yields `data: {chunk}` lines, each a partial
        # GenerateContentResponse. Emit text as it arrives; collect functionCall parts along the way.
        budget = self._thinking_budget(model, reasoning)
        level = self._thinking_level(model, reasoning)
        # Only request thought summaries when reasoning is actually on. `/think off` still floors
        # 2.5 Pro's budget at its minimum (it can't fully disable), but we must NOT surface thoughts
        # then — that would contradict the off gating and send includeThoughts alongside a 0 budget
        # on the compaction path (which streams with reasoning="off").
        include_thoughts = reasoning != "off" and (budget is not None or level is not None)
        payload = self._payload(conversation, tools, thinking_budget=budget, include_thoughts=include_thoughts,
                                thinking_level=level)
        url = f"{BASE}/models/{model}:streamGenerateContent?alt=sse"
        text_parts: list = []
        thought_parts: list = []
        calls: list = []
        last: dict = {}
        for line in post_stream(url, payload, headers=self._headers(),
                                provider=self.name, cancel=cancel):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body:
                continue
            try:
                last = json.loads(body)
            except ValueError:
                continue
            before = len(thought_parts)
            delta = self._read_parts((last.get("candidates") or [{}])[0], text_parts, calls, thought_parts)
            if on_thinking:
                for tp in thought_parts[before:]:
                    on_thinking(tp)
            if delta:
                on_text(delta)
        text = "".join(text_parts).strip()
        think = "".join(thought_parts).strip()
        return ProviderResponse(message=Message.assistant(text=text or None, thinking=think or None, tool_calls=calls), raw=last)
