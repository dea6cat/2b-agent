"""Google Gemini adapter (generateContent).

Native wire format: `contents` with role user/model and parts; tools as
functionDeclarations; functionCall / functionResponse parts. Gemini keys a
functionResponse by the tool's NAME, not an id, so we resolve each result's
tool_call_id back to the name via the tool_calls seen earlier in the
conversation.
"""
import os
from typing import Callable

from ..conversation import Conversation, Message, Role, ToolCall
from ..toolspec import ToolSpec, to_gemini
from .base import ProviderResponse, post_json

BASE = "https://generativelanguage.googleapis.com/v1beta"
_MODELS = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"]


class GoogleProvider:
    name = "google"

    @property
    def api_key(self) -> str:
        return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""

    def is_available(self) -> bool:
        return bool(self.api_key)

    def list_models(self) -> list[str]:
        return list(_MODELS)

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
                    parts.append({"functionCall": {"name": tc.name, "args": tc.arguments}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:
                contents.append({"role": "user", "parts": [{"text": m.text or ""}]})
        return contents

    def send(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...]) -> ProviderResponse:
        url = f"{BASE}/models/{model}:generateContent?key={self.api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": conversation.system_prompt}]},
            "contents": self._contents(conversation),
            "tools": to_gemini(tools),
        }
        raw = post_json(url, payload, provider=self.name)
        cand = (raw.get("candidates") or [{}])[0]
        parts = cand.get("content", {}).get("parts", []) or []
        text_parts, calls = [], []
        for p in parts:
            if "text" in p:
                text_parts.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                calls.append(ToolCall.new(name=fc.get("name", ""), arguments=fc.get("args", {})))
        text = "".join(text_parts).strip()
        return ProviderResponse(
            message=Message.assistant(text=text or None, tool_calls=calls),
            raw=raw,
        )

    def stream(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...],
               on_text: Callable[[str], None]) -> ProviderResponse:
        # Non-streaming fallback (SSE parsing not yet validated for this provider).
        resp = self.send(conversation, model, tools)
        if resp.message.text:
            on_text(resp.message.text)
        return resp
