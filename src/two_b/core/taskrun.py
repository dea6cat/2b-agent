"""State for one run_task call, and the ways a run ends: stopped, failed, or with a final answer.

The turn loop's helpers (answer, tool_turn) share one TaskRun instead of a dozen loose locals,
so the counters that gate each nudge survive across turns without being threaded through by hand."""
from dataclasses import dataclass, field
from typing import Any, Callable

from . import nudges
from .conversation import Conversation, Message
from .events import AgentEvent, EventType
from .session import Session, Task, TaskState


def _finish_stopped(task: Task, on_event: Callable[["AgentEvent"], None]) -> None:
    """Return a task to idle after the user stops it — commit whatever streamed,
    show a quiet 'Stopped.' line, no red error."""
    task.status_line = ""
    task.state = TaskState.ERROR
    task.error = "stopped"
    on_event(AgentEvent(EventType.LOG, task.id, {"text": "Stopped."}))
    on_event(AgentEvent(EventType.TASK_DONE, task.id))


def _finish_failed(task: Task, on_event: Callable[["AgentEvent"], None], reason: str) -> None:
    """Terminal failure closure. Guarantees run_task ends with a clean, non-empty
    final message — never a bare stack trace, an empty output, or (worse) an
    exception escaping the worker thread and leaving the UI waiting forever. The
    reason is always classified to a readable, non-empty string before it goes out."""
    reason = (reason or "").strip() or "unknown error"
    task.status_line = ""
    task.state = TaskState.ERROR
    task.error = reason
    on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": reason}))


def _persist_final(conv, msg) -> None:
    """Append the closing assistant answer to the conversation (Phase 0 of continuity).
    The turn loop only appends tool-call turns, never the final message, so any thread
    carried forward — via /continuity or a re-attached steer — would omit the actual
    answer. Stores a clean text-only turn (mirroring what the UI showed: text, else the
    thinking fallback) so history never carries a dangling tool_call or a blank turn."""
    if msg is None:
        return
    answer = (msg.text or "").strip() or (msg.thinking or "").strip()
    if answer:
        conv.append(Message.assistant(text=answer))


@dataclass
class TaskRun:
    """Everything one run_task call carries from turn to turn."""
    session: Session
    task: Task
    on_event: Callable[[AgentEvent], None]
    provider: Any
    model: str
    is_local: bool
    read_cap: int
    conv: Conversation
    first_turn: bool
    repo_checks: list = field(default_factory=list)   # project check commands, discovered once
    known_tools: tuple = ()     # valid tool names for this task, for coerce_tool_args
    loop_guard: nudges._LoopGuard = field(default_factory=nudges._LoopGuard)
    promise_nudges: int = 0     # times we've nudged a "said it'd call a tool but didn't" turn
    tool_calls_made: int = 0    # any tool calls dispatched this task (gates the intent-stall nudge)
    stall_nudges: int = 0       # intent-only stall nudge fires at most once
    clarify_nudges: int = 0     # "asked instead of acting" nudge fires at most once
    verify_rounds: int = 0      # host-run verify-and-fix rounds, bounded by answer.MAX_VERIFY_ROUNDS
    edit_attempts: int = 0      # edit_file/write_file calls dispatched (any outcome), across turns
    false_done_nudged: bool = False   # "declared done but no edit applied" nudge fires at most once

    def emit(self, type: EventType, payload: dict | None = None) -> None:
        self.on_event(AgentEvent(type, self.task.id, payload if payload is not None else {}))

    def stopped(self) -> None:
        _finish_stopped(self.task, self.on_event)

    def failed(self, reason: str) -> None:
        _finish_failed(self.task, self.on_event, reason)
