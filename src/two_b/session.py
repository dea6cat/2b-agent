"""In-memory session and task model for Milestone 2.

A Session holds several named top-level Tasks, one foregrounded (ACTIVE) at a
time, others QUEUED or BACKGROUNDED. Everything here is plain dataclasses so a
JSON session-resume file can be added later without restructuring (Task.history
and plan_steps are already JSON-serializable).

No execution logic lives here — that's orchestrator.py. This is pure state.
"""
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskState(str, Enum):
    QUEUED = "queued"           # created, not yet started
    ACTIVE = "active"           # foregrounded; the TUI renders its live view
    BACKGROUNDED = "backgrounded"  # thread still running, not rendered live
    DONE = "done"
    ERROR = "error"


@dataclass
class PlanStep:
    text: str
    status: str = "pending"  # pending | active | done


@dataclass
class PendingConfirmation:
    """A write/edit a backgrounded task is blocked on until foregrounded.
    The worker thread waits on `answered`; the UI sets `approved` then signals."""
    prompt: str
    diff: str
    approved: bool = False
    answered: threading.Event = field(default_factory=threading.Event)


def _short_title(text: str, words: int = 8) -> str:
    parts = text.strip().split()
    title = " ".join(parts[:words])
    return title + ("…" if len(parts) > words else "")


@dataclass
class Task:
    description: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    history: list = field(default_factory=list)      # conversation messages (raw dicts in M2)
    plan_steps: list[PlanStep] = field(default_factory=list)
    state: TaskState = TaskState.QUEUED
    status_line: str = ""                            # e.g. "Reading" / "" when idle
    turn_started_at: float = 0.0
    model_override: str | None = None                # set by /model --task scope
    thread: threading.Thread | None = None
    cancel_flag: threading.Event = field(default_factory=threading.Event)
    last_diff: str | None = None                     # for /diff
    last_edit_snapshot: tuple | None = None          # (path, pre_content_or_None) for /undo
    pending: "PendingConfirmation | None" = None     # set when blocked on a backgrounded write
    error: str | None = None

    def __post_init__(self):
        if not self.title:
            self.title = _short_title(self.description)

    def status_glyph(self) -> str:
        return {
            TaskState.QUEUED: "·",
            TaskState.ACTIVE: "▸",
            TaskState.BACKGROUNDED: "⋯",
            TaskState.DONE: "✓",
            TaskState.ERROR: "✗",
        }[self.state]

    def step_counts(self) -> tuple[int, int, int]:
        pending = sum(1 for s in self.plan_steps if s.status == "pending")
        active = sum(1 for s in self.plan_steps if s.status == "active")
        done = sum(1 for s in self.plan_steps if s.status == "done")
        return pending, active, done


@dataclass
class Session:
    default_model: str = ""
    auto_yes: bool = False
    cwd: str = "."
    tasks: list[Task] = field(default_factory=list)
    active_task_id: str | None = None
    # Events emitted by any task thread, drained by the UI thread so all
    # rendering happens on one thread regardless of which task produced it.
    events: "queue.Queue" = field(default_factory=queue.Queue)

    @property
    def active_task(self) -> Task | None:
        return next((t for t in self.tasks if t.id == self.active_task_id), None)

    def add_task(self, description: str) -> Task:
        task = Task(description=description)
        self.tasks.append(task)
        return task

    def find(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def other_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.id != self.active_task_id]
