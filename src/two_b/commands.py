"""Slash-command dispatch. Any input starting with '/' is handled here on the
UI thread and never reaches the model. Handlers operate on the App controller
(duck-typed: .session, .console, and task-control methods) to avoid a circular
import with cli.py.
"""
import os

from . import orchestrator, registry, tools
from .conversation import Conversation, Message
from .session import MODE_ACCEPT, MODE_NORMAL, MODE_PLAN, MODE_LABELS

COMMANDS = {}


def command(*names):
    def deco(fn):
        for n in names:
            COMMANDS[n] = fn
        return fn
    return deco


def command_specs() -> list[tuple[str, str]]:
    """Ordered (primary_name, one-line-doc) for each command, deduped by handler
    (aliases collapse to the first-registered name). Drives the / completion menu."""
    specs: list[tuple[str, str]] = []
    seen = set()
    for name, fn in COMMANDS.items():
        if fn in seen:
            continue
        seen.add(fn)
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        specs.append((name, doc))
    return specs


def dispatch_input(raw: str, app) -> bool:
    """Return True if the line was a handled slash command (never reaches the
    model); False if it should be treated as task input."""
    if not raw.startswith("/"):
        return False
    parts = raw[1:].split(maxsplit=1)
    if not parts:
        return False
    name, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    handler = COMMANDS.get(name)
    if handler is None:
        app.ui.print(f"[red]Unknown command:[/red] /{name}  (try /help)")
        return True
    handler(rest, app)
    return True


def _target_task(app):
    """The task a state command acts on: the active one, else the most recent."""
    return app.session.active_task or (app.session.tasks[-1] if app.session.tasks else None)


@command("help", "h")
def _help(rest, app):
    """Show this help."""
    app.ui.print("[bold]Commands:[/bold]")
    seen = set()
    for name, fn in COMMANDS.items():
        if fn in seen:
            continue
        seen.add(fn)
        doc = (fn.__doc__ or "").strip().splitlines()[0]
        app.ui.print(f"  [cyan]/{name}[/cyan] — {doc}")
    app.ui.print("  Type anything else to run it as a task.")


@command("model")
def _model(rest, app):
    """Show or switch the model. Bare name or provider:name; context is preserved."""
    if not rest.strip():
        app.ui.print(f"Current model: [bold]{app.session.default_model}[/bold]")
        return
    name = rest.strip()
    resolved = registry.resolve(app.registry, name)
    if resolved is None:
        app.ui.print(
            f"[red]Could not resolve model:[/red] {name}. "
            "It may be ambiguous — try [bold]provider:model[/bold]. See /models."
        )
        return
    provider, model = resolved
    # Context is preserved: the active task keeps its canonical Conversation and
    # simply gets re-serialized for the new provider on the next turn. We store
    # the fully-qualified 'provider:model' so resolution is unambiguous later.
    app.session.default_model = f"{provider.name}:{model}"
    active = app.session.active_task
    if active is not None:
        active.model_override = f"{provider.name}:{model}"
    app.ui.print(f"Model set to [bold]{provider.name}:{model}[/bold] (context preserved).")


@command("models")
def _models(rest, app):
    """List available models, grouped by provider (scrollable; filter: /models <text>)."""
    reg = registry.usable(app.registry)
    if not reg:
        app.ui.print("[red]No providers configured.[/red] Start Ollama or set a provider API key.")
        return
    needle = rest.strip().lower()

    def emit(out):
        total = 0
        for pname, prov in reg.items():
            try:
                models = prov.list_models()
            except Exception as e:
                out.print(f"  [red]{pname}: {e}[/red]")
                continue
            if needle:
                models = [m for m in models if needle in m.lower()]
            if not models:
                continue
            out.print(f"[bold]{pname}[/bold]")
            for m in models:
                marker = "  (current)" if app.session.default_model in (m, f"{pname}:{m}") else ""
                out.print(f"    {m}{marker}")
            total += len(models)
        if needle and total == 0:
            out.print(f"[dim]No models match '{needle}'.[/dim]")

    # Long lists (OpenRouter/NVIDIA can be hundreds) scroll through the pager.
    with app.ui.pager(styles=True):
        emit(app.ui)


@command("task")
def _task(rest, app):
    """Queue a new task: /task <description>."""
    if not rest.strip():
        app.ui.print("Usage: /task <description>")
        return
    app.enqueue_task(rest.strip())


@command("tasks")
def _tasks(rest, app):
    """List all tasks in this session and their status."""
    if not app.session.tasks:
        app.ui.print("(no tasks yet)")
        return
    for t in app.session.tasks:
        pend, act, done = t.step_counts()
        steps = f"  [{done}✓/{act}▶/{pend}□]" if t.plan_steps else ""
        wait = "  [waiting for confirmation]" if t.pending else ""
        app.ui.print(f"  {t.status_glyph()} [{t.id}] {t.title}  ({t.state.value}){steps}{wait}")


@command("fg")
def _fg(rest, app):
    """Foreground a backgrounded task by id: /fg <id> (needed to approve a task waiting on a write)."""
    if not rest.strip():
        app.ui.print("Usage: /fg <id>  (see /tasks for ids)")
        return
    task = app.session.find(rest.strip())
    if task is None:
        app.ui.print(f"[red]No task with id {rest.strip()}[/red]")
        return
    app.request_foreground(task.id)


@command("yes")
def _yes(rest, app):
    """Toggle accept-edits mode (auto-approve writes/edits) for the session."""
    s = app.session
    s.mode = MODE_NORMAL if s.mode == MODE_ACCEPT else MODE_ACCEPT
    app.ui.print(f"Mode: [bold]{MODE_LABELS[s.mode]}[/bold]")


_MODE_ALIASES = {
    "normal": MODE_NORMAL, "confirm": MODE_NORMAL,
    "accept": MODE_ACCEPT, "accept_edits": MODE_ACCEPT, "accept-edits": MODE_ACCEPT,
    "edits": MODE_ACCEPT, "yes": MODE_ACCEPT, "auto": MODE_ACCEPT,
    "plan": MODE_PLAN,
}


@command("mode")
def _mode(rest, app):
    """Set operating mode: /mode [normal|accept|plan] (or shift+tab to cycle)."""
    s = app.session
    name = rest.strip().lower()
    if not name:
        app.ui.print(f"Current mode: [bold]{MODE_LABELS[s.mode]}[/bold].  "
                     "Options: normal (confirm), accept (auto-apply), plan (read-only).")
        return
    target = _MODE_ALIASES.get(name)
    if target is None:
        app.ui.print(f"[red]Unknown mode '{name}'.[/red] Options: normal, accept, plan.")
        return
    s.mode = target
    app.ui.print(f"Mode: [bold]{MODE_LABELS[s.mode]}[/bold]")


@command("undo")
def _undo(rest, app):
    """Revert the last file write/edit (single level)."""
    task = _target_task(app)
    if task is None or task.last_edit_snapshot is None:
        app.ui.print("Nothing to undo.")
        return
    path, pre = task.last_edit_snapshot
    full = tools._safe_path(path)
    if full is None:
        app.ui.print("[red]Cannot undo: empty or invalid path.[/red]")
        return
    if pre is None:
        # It was a newly created file — undo means remove it.
        try:
            os.remove(full)
            app.ui.print(f"Removed newly-created {path}.")
        except OSError as e:
            app.ui.print(f"[red]Undo failed: {e}[/red]")
    else:
        with open(full, "w") as f:
            f.write(pre)
        app.ui.print(f"Reverted {path} to its previous contents.")
    task.last_edit_snapshot = None
    task.last_diff = None


@command("diff")
def _diff(rest, app):
    """Re-show the last proposed/applied diff."""
    task = _target_task(app)
    if task is None or not task.last_diff:
        app.ui.print("No diff available.")
        return
    app.ui.print(task.last_diff)


@command("add")
def _add(rest, app):
    """Pre-load a file into the current task's context: /add <file>."""
    if not rest.strip():
        app.ui.print("Usage: /add <file>")
        return
    task = _target_task(app)
    if task is None:
        app.ui.print("No task to add context to — start a task first.")
        return
    content = tools.do_read_file(rest.strip())
    if content.startswith("error:"):
        app.ui.print(f"[red]{content}[/red]")
        return
    if task.conversation is None:
        task.conversation = Conversation(system_prompt=orchestrator.SYSTEM_PROMPT)
    task.conversation.append(Message.user(f"[pre-loaded file: {rest.strip()}]\n{content}"))
    app.ui.print(f"Loaded {rest.strip()} into task context ({len(content)} bytes).")


@command("theme")
def _theme(rest, app):
    """Switch color theme: /theme [system|light|dark] (system = terminal background)."""
    name = rest.strip().lower()
    if not name:
        current = getattr(app, "theme_name", "system")
        app.ui.print(f"Current theme: [bold]{current}[/bold].  Options: system, light, dark.")
        return
    if not hasattr(app, "set_theme"):
        app.ui.print("Themes apply to the full-screen TUI (not [bold]--classic[/bold] mode).")
        return
    app.set_theme(name)


@command("context")
def _context(rest, app):
    """Show estimated context usage for the current task (auto-compacts near the limit)."""
    task = _target_task(app)
    if task is None or task.conversation is None:
        app.ui.print("No conversation yet — start a task first.")
        return
    resolved = registry.resolve(app.registry, task.model_override or app.session.default_model)
    budget = orchestrator.context_budget(resolved[0], resolved[1]) if resolved else 8000
    used = orchestrator.estimate_tokens(task.conversation)
    pct = int(used / budget * 100) if budget else 0
    at = int(orchestrator.COMPACT_AT * 100)
    app.ui.print(f"Context: ~[bold]{used}[/bold] / {budget} tokens ([bold]{pct}%[/bold]). "
                 f"Auto-compacts at {at}%.")


@command("copy", "cp")
def _copy(rest, app):
    """Copy the last model reply to the clipboard (or hold Option and drag to select)."""
    if hasattr(app, "copy_last"):
        app.copy_last()
    else:
        app.ui.print("In --classic mode, use your terminal's native text selection to copy.")


@command("clear")
def _clear(rest, app):
    """Reset the current task's conversation history (keeps other tasks)."""
    task = _target_task(app)
    if task is None:
        app.ui.print("No task to clear.")
        return
    task.conversation = None
    task.plan_steps.clear()
    app.ui.print(f"Cleared history for [{task.id}] {task.title}.")


@command("quit", "q", "exit")
def _quit(rest, app):
    """Exit 2B."""
    app.request_quit()
