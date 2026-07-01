"""The agentic turn loop, restructured to emit events instead of printing.

This is a faithful port of the prototype's run() loop — same Ollama native
/api/chat call, same message list, same tool dispatch, same MAX_TURNS, same
content->thinking fallback. The ONLY structural change is that progress is
reported through an on_event callback rather than print(), so a TUI (or any
other observer) can render around the unchanged model I/O path.

Provider abstraction (Anthropic/OpenAI) arrives in Milestone 3; for now this
talks to Ollama's native endpoint directly, exactly as the prototype did.
"""
import json
import os
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from . import tools

MAX_TURNS = 12
DEFAULT_MODEL = "qwen3.5:9b"


def ollama_host() -> str:
    """Endpoint precedence: OLLAMA_API_BASE (what the user's shell actually
    exports) first, then OLLAMA_HOST (what the prototype read), then default.
    Fixes the prototype's latent bug where OLLAMA_API_BASE was ignored."""
    return (
        os.environ.get("OLLAMA_API_BASE")
        or os.environ.get("OLLAMA_HOST")
        or "http://localhost:11434"
    )


SYSTEM_PROMPT = (
    "You are a careful local coding assistant with five tools: list_files, read_file, "
    "search_files, edit_file, write_file. Explore before answering or editing — use "
    "search_files to find where something is defined or used instead of guessing paths. "
    "For changes to existing files, prefer edit_file (an exact old_text/new_text "
    "replacement) over write_file — it's faster and safer, especially on large files. "
    "Only use write_file for new files or small existing ones. When finished, reply with "
    "a plain-text final answer and make no further tool calls."
)


class EventType(Enum):
    TURN_START = "turn_start"            # about to call the model
    ASSISTANT_TEXT = "assistant_text"    # final plain-text answer (no tool calls)
    TOOL_CALL_START = "tool_call_start"  # model requested a tool
    TOOL_CALL_RESULT = "tool_call_result"
    TASK_DONE = "task_done"
    TASK_ERROR = "task_error"


@dataclass
class AgentEvent:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)


def call_ollama(model: str, messages: list[dict]) -> dict:
    payload = json.dumps(
        {"model": model, "messages": messages, "tools": tools.TOOLS, "stream": False}
    ).encode()
    req = urllib.request.Request(
        f"{ollama_host()}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


def list_installed_models() -> list[str]:
    req = urllib.request.Request(f"{ollama_host()}/api/tags")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return [m["name"] for m in data.get("models", [])]


def pick_default_model() -> str:
    models = list_installed_models()
    if not models:
        raise SystemExit(
            f"No models installed in Ollama at {ollama_host()}. Run 'ollama pull {DEFAULT_MODEL}' first."
        )
    if DEFAULT_MODEL in models:
        return DEFAULT_MODEL
    return models[0]


def _dispatch_tool(name: str, args: dict, auto_yes: bool) -> str:
    if name == "list_files":
        return tools.do_list_files(args.get("path", "."))
    if name == "read_file":
        return tools.do_read_file(args["path"])
    if name == "search_files":
        return tools.do_search_files(args["query"], args.get("path", "."))
    if name == "edit_file":
        return tools.do_edit_file(args["path"], args["old_text"], args["new_text"], auto_yes)
    if name == "write_file":
        return tools.do_write_file(args["path"], args["content"], auto_yes)
    return f"error: unknown tool {name}"


def run_task(
    model: str,
    task_text: str,
    auto_yes: bool,
    on_event: Callable[[AgentEvent], None],
    history: list[dict] | None = None,
) -> None:
    """Drive the tool-call loop for one task, reporting progress via on_event.

    `history` is mutated in place (system + user prepended if empty) so callers
    can retain the conversation across turns — this is the seam Milestone 2's
    multi-task Session and Milestone 3's canonical conversation build on.
    """
    messages = history if history is not None else []
    if not messages:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    messages.append({"role": "user", "content": task_text})

    for _ in range(MAX_TURNS):
        on_event(AgentEvent(EventType.TURN_START))
        try:
            resp = call_ollama(model, messages)
        except Exception as e:  # network/URL errors, JSON errors
            on_event(AgentEvent(EventType.TASK_ERROR, {"error": str(e)}))
            return

        msg = resp.get("message", {})
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            final = (msg.get("content") or "").strip()
            if not final:
                # qwen3.5 and other reasoning models sometimes leave content
                # empty and put the real answer in `thinking`.
                final = (msg.get("thinking") or "").strip() or "(model returned an empty response)"
            on_event(AgentEvent(EventType.ASSISTANT_TEXT, {"text": final}))
            on_event(AgentEvent(EventType.TASK_DONE))
            return

        messages.append(msg)
        for call in tool_calls:
            fn = call["function"]["name"]
            args = call["function"]["arguments"]
            if isinstance(args, str):
                args = json.loads(args)
            on_event(AgentEvent(EventType.TOOL_CALL_START, {"name": fn, "arguments": args}))
            result = _dispatch_tool(fn, args, auto_yes)
            on_event(AgentEvent(EventType.TOOL_CALL_RESULT, {"name": fn, "result": result}))
            messages.append({"role": "tool", "content": result})

    on_event(AgentEvent(EventType.TASK_ERROR, {"error": "max turns reached without a final answer"}))
