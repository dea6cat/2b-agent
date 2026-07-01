"""The agentic turn loop, threaded and event-emitting (Milestone 2).

Faithful port of the prototype's loop — same Ollama native /api/chat call, same
message list, same tool dispatch, same MAX_TURNS, same content->thinking
fallback. Structural changes for M2, all host-side (the model's world is
unchanged):

  - A task runs on its own worker thread. The worker NEVER writes to the
    terminal: every print() the verbatim do_* tools emit is captured via
    redirect_stdout and shipped to the UI thread as an event. The UI thread is
    the sole owner of stdout / rich.Live / input(). This is what lets the tools
    stay byte-for-byte unchanged while running off the main thread.
  - Write/edit confirmations are routed to the UI thread through the task's
    PendingConfirmation (see request_confirmation). A backgrounded task blocks
    there until it is foregrounded — the "pause on write" behavior.
  - Plan steps are parsed from the model's own first-turn text and their
    active/done state inferred from tool calls (planparse), purely for display.

Provider abstraction (Anthropic/OpenAI) is Milestone 3; this still speaks
Ollama's native protocol directly.
"""
import io
import json
import os
import urllib.request
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from . import planparse, tools
from .session import PendingConfirmation, Session, Task, TaskState

MAX_TURNS = 12
DEFAULT_MODEL = "qwen3.5:9b"

BASE_SYSTEM_PROMPT = (
    "You are a careful local coding assistant with five tools: list_files, read_file, "
    "search_files, edit_file, write_file. Explore before answering or editing — use "
    "search_files to find where something is defined or used instead of guessing paths. "
    "For changes to existing files, prefer edit_file (an exact old_text/new_text "
    "replacement) over write_file — it's faster and safer, especially on large files. "
    "Only use write_file for new files or small existing ones. When finished, reply with "
    "a plain-text final answer and make no further tool calls."
)
SYSTEM_PROMPT = BASE_SYSTEM_PROMPT + planparse.PLAN_PROMPT

_STATUS = {
    "list_files": "Listing files",
    "read_file": "Reading",
    "search_files": "Searching",
    "edit_file": "Editing",
    "write_file": "Writing",
}


def ollama_host() -> str:
    """OLLAMA_API_BASE (what the user's shell exports) first, then OLLAMA_HOST
    (what the prototype read), then default. Fixes the prototype's latent bug."""
    return (
        os.environ.get("OLLAMA_API_BASE")
        or os.environ.get("OLLAMA_HOST")
        or "http://localhost:11434"
    )


class EventType(Enum):
    TURN_START = "turn_start"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_RESULT = "tool_call_result"
    LOG = "log"                # captured tool stdout, to print to scrollback
    TASK_DONE = "task_done"
    TASK_ERROR = "task_error"


@dataclass
class AgentEvent:
    type: EventType
    task_id: str
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


# --- confirmation routed to the UI thread -----------------------------------

def request_confirmation(session: Session, task: Task, prompt: str, diff: str) -> bool:
    """Called from a worker thread. If auto-approve is on, approve immediately.
    Otherwise hand a PendingConfirmation to the UI thread and block until it is
    answered (a backgrounded task simply waits here until foregrounded)."""
    if session.auto_yes:
        return True
    pc = PendingConfirmation(prompt=prompt, diff=diff)
    task.pending = pc
    try:
        while not pc.answered.wait(timeout=0.2):
            if task.cancel_flag.is_set():
                return False
        return pc.approved
    finally:
        task.pending = None


# --- write/edit wrappers: snapshot for /undo, confirm via UI, then apply -----

def apply_write(session: Session, task: Task, path: str, content: str) -> str:
    full = tools._safe_path(path)
    pre = None
    if full and os.path.isfile(full):
        with open(full, "r", errors="replace") as f:
            pre = f.read()
    normalized = content if content.endswith("\n") or not content else content + "\n"
    preview = f"(full overwrite of {path}: {len(normalized.splitlines())} lines)"
    if not request_confirmation(session, task, f"Apply write to {path}?", preview):
        return "write rejected by user"
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = tools.do_write_file(path, content, auto_yes=True)
    if result.startswith("wrote"):
        task.last_edit_snapshot = (path, pre)
        task.last_diff = preview
    return result


def apply_edit(session: Session, task: Task, path: str, old_text: str, new_text: str) -> str:
    full = tools._safe_path(path)
    if full is None:
        return "error: path escapes working directory"
    if not os.path.isfile(full):
        return f"error: no such file: {path}"
    with open(full, "r", errors="replace") as f:
        pre = f.read()
    count = pre.count(old_text)
    if count == 0:
        return "error: old_text not found in file — it must match exactly, including whitespace"
    if count > 1:
        return f"error: old_text matches {count} times — make it more specific so it matches exactly once"
    import difflib

    new_content = pre.replace(old_text, new_text, 1)
    diff = "\n".join(difflib.unified_diff(pre.splitlines(), new_content.splitlines(), lineterm="", n=1))
    if not request_confirmation(session, task, f"Apply edit to {path}?", diff):
        return "edit rejected by user"
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = tools.do_edit_file(path, old_text, new_text, auto_yes=True)
    if result.startswith("edited"):
        task.last_edit_snapshot = (path, pre)
        task.last_diff = diff
    return result


def _dispatch_tool(session: Session, task: Task, name: str, args: dict) -> str:
    if name == "edit_file":
        return apply_edit(session, task, args["path"], args["old_text"], args["new_text"])
    if name == "write_file":
        return apply_write(session, task, args["path"], args["content"])
    # read-only tools: capture any stray stdout, none expected
    buf = io.StringIO()
    with redirect_stdout(buf):
        if name == "list_files":
            return tools.do_list_files(args.get("path", "."))
        if name == "read_file":
            return tools.do_read_file(args["path"])
        if name == "search_files":
            return tools.do_search_files(args["query"], args.get("path", "."))
    return f"error: unknown tool {name}"


# --- the turn loop -----------------------------------------------------------

def run_task(session: Session, task: Task, on_event: Callable[[AgentEvent], None]) -> None:
    """Drive the tool-call loop for one task on the calling (worker) thread.
    Emits events for the UI thread; never writes the terminal itself."""
    import time

    model = task.model_override or session.default_model
    messages = task.history
    if not messages:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    messages.append({"role": "user", "content": task.description})

    first_turn = True
    try:
        for _ in range(MAX_TURNS):
            if task.cancel_flag.is_set():
                task.state = TaskState.ERROR
                task.error = "cancelled"
                on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": "cancelled"}))
                return

            task.status_line = "Thinking"
            task.turn_started_at = time.monotonic()
            on_event(AgentEvent(EventType.TURN_START, task.id))

            try:
                resp = call_ollama(model, messages)
            except Exception as e:
                task.state = TaskState.ERROR
                task.error = str(e)
                on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": str(e)}))
                return

            msg = resp.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()

            if first_turn and content:
                parsed = planparse.extract_plan(content)
                if parsed:
                    task.plan_steps = parsed
            first_turn = False

            if not tool_calls:
                final = content
                if not final:
                    final = (msg.get("thinking") or "").strip() or "(model returned an empty response)"
                planparse.finalize_steps(task.plan_steps)
                task.status_line = ""
                task.state = TaskState.DONE
                on_event(AgentEvent(EventType.ASSISTANT_TEXT, task.id, {"text": final}))
                on_event(AgentEvent(EventType.TASK_DONE, task.id))
                return

            messages.append(msg)
            for call in tool_calls:
                fn = call["function"]["name"]
                args = call["function"]["arguments"]
                if isinstance(args, str):
                    args = json.loads(args)
                planparse.infer_active_step(task.plan_steps, fn, args)
                task.status_line = _STATUS.get(fn, "Working")
                shown = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in args.items()}
                on_event(AgentEvent(EventType.TOOL_CALL_START, task.id, {"name": fn, "shown": shown}))
                result = _dispatch_tool(session, task, fn, args)
                on_event(AgentEvent(EventType.TOOL_CALL_RESULT, task.id, {"name": fn, "result": result}))
                messages.append({"role": "tool", "content": result})

        task.state = TaskState.ERROR
        task.error = "max turns reached without a final answer"
        on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": task.error}))
    finally:
        if task.status_line:
            task.status_line = ""
