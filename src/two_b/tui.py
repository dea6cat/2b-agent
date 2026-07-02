"""Rendering for 2B's live view (Milestone 2: plan checklist + multi-task).

Pure presentation: these functions read Session/Task state and return rich
renderables. They never mutate state, touch the model, or block. The UI loop in
cli.py owns the rich.Live region and calls render_session() on each refresh;
the orchestrator owns updating task.status_line / plan step states.
"""
import time

from rich.console import Group
from rich.text import Text
from rich.tree import Tree

from .session import Session, Task, TaskState

VISIBLE_STEPS = 5
_SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _spinner_frame() -> str:
    # Animate off wall-clock time so the frame advances on every render,
    # regardless of how the renderable is rebuilt (a fresh rich.Spinner would
    # reset to frame 0 each call and appear frozen).
    return _SPIN_FRAMES[int(time.monotonic() * 12) % len(_SPIN_FRAMES)]

_STEP_STYLE = {
    "done": ("✓", "green"),
    "active": ("■", "bold"),
    "pending": ("□", "dim"),
}


def _elapsed(task: Task) -> str:
    if not task.turn_started_at:
        return ""
    return f"({int(time.monotonic() - task.turn_started_at)}s)"


def render_task(task: Task) -> Group:
    """The foregrounded task's live view: status line (spinner while working)
    plus the plan checklist if one was parsed."""
    lines: list = []

    if task.status_line:
        perf = f"  ·  {task.perf}" if task.perf else ""
        lines.append(Text(f"{_spinner_frame()}  {task.status_line}… {_elapsed(task)}{perf}"))
        lines.append(Text("  (ctrl+b to run in background)", style="dim"))

    if task.plan_steps:
        root = Text(f"{task.title}  {_elapsed(task)}", style="bold")
        tree = Tree(root, guide_style="dim")
        for step in task.plan_steps[:VISIBLE_STEPS]:
            glyph, style = _STEP_STYLE[step.status]
            tree.add(Text(f"{glyph} {step.text}", style=style))
        hidden = task.plan_steps[VISIBLE_STEPS:]
        if hidden:
            n_pending = sum(1 for s in hidden if s.status == "pending")
            n_done = sum(1 for s in hidden if s.status == "done")
            tree.add(Text(f"… +{n_pending} pending, {n_done} completed", style="dim italic"))
        lines.append(tree)

    if not lines:
        lines.append(Text(f"{task.title}  {_elapsed(task)}", style="dim"))
    return Group(*lines)


def render_session(session: Session) -> Group:
    """Full live view: the active task's detail, then a dim one-line summary of
    every other task so the user can glance at what else is queued/running."""
    parts: list = []
    active = session.active_task
    if active is not None:
        parts.append(render_task(active))

    others = session.other_tasks()
    if others:
        parts.append(Text(""))
        for t in others:
            suffix = ""
            if t.state == TaskState.BACKGROUNDED and t.pending is not None:
                suffix = "  [needs confirmation — foreground to approve]"
            parts.append(
                Text(f"  {t.status_glyph()} {t.title}  [{t.state.value}]{suffix}", style="dim")
            )

    if not parts:
        parts.append(Text("(idle — type a task, or /help)", style="dim"))
    return Group(*parts)
