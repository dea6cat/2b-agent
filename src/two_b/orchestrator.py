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

Milestone 3: the model I/O now goes through a provider adapter (resolved from
the registry by the active model), driven by the canonical Conversation. The
loop, confirmation routing, plan parsing, and events are unchanged — only the
transport is abstracted. Local Ollama still reaches its native /api/chat.
"""
import io
import os
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from . import planparse, registry, tools
from .conversation import Conversation, Message, ToolResult
from .providers.base import ProviderError
from .session import PendingConfirmation, Session, Task, TaskState
from .toolspec import TOOL_SPECS

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


class EventType(Enum):
    TURN_START = "turn_start"
    ASSISTANT_DELTA = "assistant_delta"    # a streamed chunk of the reply
    ASSISTANT_TEXT = "assistant_text"      # (legacy; kept for non-stream callers)
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


def pick_default_model() -> str:
    """Default model at startup: prefer local Ollama's qwen3.5:9b if present,
    else the first local model. (Cloud providers aren't auto-defaulted.)"""
    reg = registry.build_registry()
    ol = reg.get("ollama")
    models = []
    if ol is not None:
        try:
            models = ol.list_models()
        except Exception:
            models = []
    if not models:
        raise SystemExit(
            f"No local Ollama models found. Run 'ollama pull {DEFAULT_MODEL}' first, "
            "or configure a cloud provider (set an API key) and pass --model provider:name."
        )
    return DEFAULT_MODEL if DEFAULT_MODEL in models else models[0]


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

def run_task(session: Session, task: Task, on_event: Callable[[AgentEvent], None],
             reg: dict | None = None) -> None:
    """Drive the tool-call loop for one task on the calling (worker) thread.
    Resolves the active model to a provider, then drives it via the canonical
    Conversation. Emits events for the UI thread; never writes the terminal."""
    import time

    reg = reg if reg is not None else registry.build_registry()
    model_str = task.model_override or session.default_model
    resolved = registry.resolve(reg, model_str)
    if resolved is None:
        err = f"could not resolve model '{model_str}' to a configured provider (try /models)"
        task.state = TaskState.ERROR
        task.error = err
        on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": err}))
        return
    provider, model = resolved

    if task.conversation is None:
        task.conversation = Conversation(system_prompt=SYSTEM_PROMPT)
    conv = task.conversation
    conv.append(Message.user(task.description))

    first_turn = not any(m.role.value == "assistant" for m in conv.messages)
    try:
        for _ in range(MAX_TURNS):
            if task.cancel_flag.is_set():
                task.state = TaskState.ERROR
                task.error = "cancelled"
                on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": "cancelled"}))
                return

            task.status_line = "Thinking"
            task.turn_started_at = time.monotonic()
            # Best-effort perf readout (local models). May be blank on the very
            # first turn until the model finishes loading; refreshed on the first
            # streamed token below, and persists across turns once set.
            if hasattr(provider, "perf"):
                try:
                    p = provider.perf(model)
                    if p:
                        task.perf = p
                except Exception:
                    pass
            on_event(AgentEvent(EventType.TURN_START, task.id))

            streamed = {"n": 0, "perf": False}

            def on_text(chunk: str, _t=task, _p=provider, _m=model) -> None:
                if not streamed["perf"] and hasattr(_p, "perf"):
                    streamed["perf"] = True
                    try:
                        val = _p.perf(_m)
                        if val:
                            _t.perf = val
                    except Exception:
                        pass
                streamed["n"] += len(chunk)
                on_event(AgentEvent(EventType.ASSISTANT_DELTA, _t.id, {"chunk": chunk}))

            try:
                resp = provider.stream(conv, model, TOOL_SPECS, on_text)
            except ProviderError as e:
                task.state = TaskState.ERROR
                task.error = str(e)
                on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": str(e)}))
                return
            except Exception as e:
                task.state = TaskState.ERROR
                task.error = str(e)
                on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": str(e)}))
                return

            msg = resp.message
            content = (msg.text or "").strip()

            if first_turn and content:
                parsed = planparse.extract_plan(content)
                if parsed:
                    task.plan_steps = parsed
            first_turn = False

            if not msg.tool_calls:
                planparse.finalize_steps(task.plan_steps)
                task.status_line = ""
                task.state = TaskState.DONE
                # If nothing streamed (e.g. answer landed in `thinking`), emit it now.
                if streamed["n"] == 0:
                    fallback = content or (msg.thinking or "").strip() or "(model returned an empty response)"
                    on_event(AgentEvent(EventType.ASSISTANT_DELTA, task.id, {"chunk": fallback}))
                on_event(AgentEvent(EventType.TASK_DONE, task.id))
                return

            conv.append(msg)
            results = []
            for tc in msg.tool_calls:
                planparse.infer_active_step(task.plan_steps, tc.name, tc.arguments)
                task.status_line = _STATUS.get(tc.name, "Working")
                shown = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in tc.arguments.items()}
                on_event(AgentEvent(EventType.TOOL_CALL_START, task.id, {"name": tc.name, "shown": shown}))
                result = _dispatch_tool(session, task, tc.name, tc.arguments)
                on_event(AgentEvent(EventType.TOOL_CALL_RESULT, task.id, {"name": tc.name, "result": result}))
                results.append(ToolResult(tool_call_id=tc.id, content=result))
            conv.append(Message.results(results))

        task.state = TaskState.ERROR
        task.error = "max turns reached without a final answer"
        on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": task.error}))
    finally:
        if task.status_line:
            task.status_line = ""
