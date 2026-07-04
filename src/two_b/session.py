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

from .conversation import Conversation


class TaskState(str, Enum):
    QUEUED = "queued"           # created, not yet started
    ACTIVE = "active"           # foregrounded; the TUI renders its live view
    BACKGROUNDED = "backgrounded"  # thread still running, not rendered live
    DONE = "done"
    ERROR = "error"


# Operating modes, cycled with shift+tab (or set via /mode). These change how the
# write-gated tools behave — 2B's only confirm-gated tools are edit_file/write_file,
# so there's no separate "auto" mode (it would be identical to accept-edits).
MODE_NORMAL = "normal"          # confirm every write/edit
MODE_ACCEPT = "accept_edits"    # auto-approve writes/edits
MODE_PLAN = "plan"              # read-only: edits/writes refused; the model plans instead
MODES = (MODE_NORMAL, MODE_ACCEPT, MODE_PLAN)
MODE_LABELS = {MODE_NORMAL: "normal mode", MODE_ACCEPT: "accept edits", MODE_PLAN: "plan mode"}


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
    grant_key: str | None = None   # e.g. "edit_file" — "allow for session" remembers this


def _short_title(text: str, words: int = 8) -> str:
    parts = text.strip().split()
    title = " ".join(parts[:words])
    return title + ("…" if len(parts) > words else "")


@dataclass
class Task:
    description: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    conversation: Conversation | None = None         # canonical history (M3); built on first run
    plan_steps: list[PlanStep] = field(default_factory=list)
    state: TaskState = TaskState.QUEUED
    status_line: str = ""                            # e.g. "Reading" / "" when idle
    perf: str = ""                                    # local-model RAM/GPU readout, e.g. "5.6GB · 100% GPU"
    turn_started_at: float = 0.0
    model_override: str | None = None                # set by /model --task scope
    thread: threading.Thread | None = None
    cancel_flag: threading.Event = field(default_factory=threading.Event)
    last_diff: str | None = None                     # for /diff
    edit_history: list = field(default_factory=list)  # stack of (path, pre_content_or_None) for multi-level /undo
    read_mtimes: dict = field(default_factory=dict)  # abspath -> mtime when last read, for stale-edit detection
    last_read_arg: str | None = None                 # exact path arg of the last read_file, for read dedup/loop-guard
    read_repeat: int = 0                             # consecutive identical unchanged reads (read-loop circuit breaker)
    pending: "PendingConfirmation | None" = None     # set when blocked on a backgrounded write
    error: str | None = None
    last_compact_tokens: int = 0                     # size floor after the last compaction (anti-thrash)
    steer_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _steer: list[str] = field(default_factory=list)  # mid-turn user redirects awaiting the next tool boundary

    def __post_init__(self):
        if not self.title:
            self.title = _short_title(self.description)

    # --- steer: fold a mid-turn user message into the running turn --------------
    # The UI thread pushes text the user typed while this task was running; the worker
    # thread drains it at the next tool-batch boundary. Guarded by a lock since the two
    # threads touch the buffer concurrently.
    def push_steer(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self.steer_lock:
            self._steer.append(text)

    def take_steer(self) -> str:
        """Return all pending steer text (newline-joined) and clear the buffer; '' if none."""
        with self.steer_lock:
            if not self._steer:
                return ""
            out = "\n".join(self._steer)
            self._steer.clear()
            return out

    def clear_steer(self) -> None:
        with self.steer_lock:
            self._steer.clear()

    def push_edit(self, path: str, pre: str | None) -> None:
        """Record a file's pre-edit content on the undo stack (newest last), capped so
        a long session can't grow it without bound. `pre` is None for a new file."""
        self.edit_history.append((path, pre))
        if len(self.edit_history) > 50:
            self.edit_history.pop(0)

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
    auto_yes: bool = False       # legacy seed for the initial mode (--yes / /yes)
    cwd: str = "."
    mode: str = MODE_NORMAL
    tasks: list[Task] = field(default_factory=list)
    active_task_id: str | None = None
    granted: set = field(default_factory=set)    # tool keys "allowed for this session" (skip confirm)
    # Events emitted by any task thread, drained by the UI thread so all
    # rendering happens on one thread regardless of which task produced it.
    events: "queue.Queue" = field(default_factory=queue.Queue)

    def __post_init__(self):
        # --yes / auto_yes at startup is just "begin in accept-edits mode".
        if self.auto_yes and self.mode == MODE_NORMAL:
            self.mode = MODE_ACCEPT
        # Pre-granted tools from config (`allowed_tools`) are never confirmed — the
        # persistent form of "allow for this session". Best-effort, lazy import.
        try:
            from . import config
            self.granted.update(config.get_prefs().get("allowed_tools", []) or [])
        except Exception:
            pass

    @property
    def approve_writes(self) -> bool:
        """Whether writes/edits apply without a confirmation prompt."""
        return self.mode == MODE_ACCEPT

    @property
    def read_only(self) -> bool:
        """Plan mode — edit_file/write_file are refused; the model plans instead."""
        return self.mode == MODE_PLAN

    def cycle_mode(self) -> str:
        i = MODES.index(self.mode) if self.mode in MODES else 0
        self.mode = MODES[(i + 1) % len(MODES)]
        return self.mode

    def set_mode(self, name: str) -> bool:
        if name in MODES:
            self.mode = name
            return True
        return False

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
