"""Canonical, provider-agnostic conversation model.

This is the single source of truth for a task's history. It is a DATA MODEL,
never a wire format — no provider's JSON shape ever lives here. Each provider
adapter serializes this fresh into its own native format on every request and
normalizes responses back into these types. That re-derivation is exactly what
makes switching models mid-task safe: history is preserved as canonical data
and re-expressed for whichever provider is now active.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(slots=True)
class ToolCall:
    """A single tool invocation the assistant requested. `id` is always present
    (synthesized when a provider — e.g. Ollama's native API — doesn't supply
    one), so tool_use/tool_result pairing works across providers."""
    id: str
    name: str
    arguments: dict[str, Any]

    @staticmethod
    def new(name: str, arguments: dict[str, Any], id: str | None = None) -> "ToolCall":
        return ToolCall(id=id or f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=dict(arguments or {}))


@dataclass(slots=True)
class ToolResult:
    """The result of executing a ToolCall, keyed back by id. Results are always
    flattened to text for this tool set."""
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class Message:
    """One turn. The role determines which fields are meaningful; kept flat
    (not subclassed) so it stays trivially JSON-serializable for session save."""
    role: Role
    text: str | None = None                      # user text, or assistant's visible reply
    thinking: str | None = None                  # assistant reasoning, if the model exposed it
    tool_calls: list[ToolCall] = field(default_factory=list)     # assistant requesting tools
    tool_results: list[ToolResult] = field(default_factory=list)  # a turn carrying results back

    @staticmethod
    def user(text: str) -> "Message":
        return Message(role=Role.USER, text=text)

    @staticmethod
    def assistant(text: str | None = None, thinking: str | None = None,
                  tool_calls: list[ToolCall] | None = None) -> "Message":
        return Message(role=Role.ASSISTANT, text=text, thinking=thinking, tool_calls=tool_calls or [])

    @staticmethod
    def results(results: list[ToolResult]) -> "Message":
        # Canonically a user-role turn carrying tool results; each adapter
        # decides how to encode it (Anthropic: user w/ tool_result blocks;
        # OpenAI/Ollama: role=tool per result; Gemini: functionResponse parts).
        return Message(role=Role.USER, tool_results=list(results))


@dataclass(slots=True)
class Conversation:
    """The full task history — the source of truth `/model` re-serializes."""
    system_prompt: str
    messages: list[Message] = field(default_factory=list)

    def append(self, message: Message) -> None:
        self.messages.append(message)
