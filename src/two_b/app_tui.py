"""Full-screen Textual TUI for 2B.

A persistent alternate-screen app: a scrolling conversation log, a live plan
checklist, a framed input box, and a status bar (model · perf · shortcuts).

The engine is untouched — this consumes the orchestrator's existing event
stream (worker threads push AgentEvents into session.events) and renders them
into widgets. It implements the same duck-typed interface commands.py expects
(session, registry, console, enqueue_task, request_quit, request_foreground),
so slash commands work unchanged, with console output routed into the log.

Interactive terminals get this by default; scripted/piped runs and `--classic`
use the line-mode REPL in cli.py.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from . import orchestrator, registry, theme
from .commands import command_specs, dispatch_input
from .orchestrator import EventType
from .session import MODE_ACCEPT, MODE_LABELS, MODE_PLAN, Session, TaskState
from .tui import VISIBLE_STEPS

# Mode indicator glyph + accent color (fixed hues that read on every theme).
_MODE_STYLE = {
    MODE_ACCEPT: ("▶▶", "#A78BD0"),   # accept edits — purple
    MODE_PLAN: ("❚❚", "#5FA69C"),     # plan mode — teal
}


class ConfirmScreen(ModalScreen[bool]):
    """Modal shown when a task needs a write/edit confirmed. Returns True/False."""
    CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: 80%; max-width: 110; height: auto; border: round #8A7A45;
           background: #C7C1AE; color: #454235; padding: 1 2; }
    #q { color: #454235; padding-top: 1; }
    #btns { height: auto; padding-top: 1; align-horizontal: right; }
    Button { margin-left: 2; }
    """

    def __init__(self, prompt: str, diff: str):
        super().__init__()
        self._prompt = prompt
        self._diff = diff

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(self._diff or "(no preview)")
            yield Static(f"{self._prompt}  (y / n)", id="q")
            with Horizontal(id="btns"):
                yield Button("Apply", variant="success", id="apply")
                yield Button("Cancel", variant="error", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "apply")

    def on_key(self, event) -> None:
        if event.key == "y":
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            self.dismiss(False)

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _describe_tool(name: str, args: dict) -> str:
    """Turn a tool call into a conversational action phrase."""
    p = args.get("path")
    if name == "read_file":
        return f"Reading {p}"
    if name == "list_files":
        return f"Listing {p or '.'}"
    if name == "search_files":
        q = args.get("query", "")
        where = f" in {p}" if p and p != "." else ""
        return f'Searching for "{q}"{where}'
    if name == "edit_file":
        return f"Editing {p}"
    if name == "write_file":
        return f"Writing {p}"
    return name


class _LogConsole:
    """A console shim so commands.py's `app.console.print(...)` writes into the
    scrolling RichLog. pager() is a no-op (the log scrolls natively)."""

    def __init__(self, app: "TwoBApp"):
        self._app = app

    def print(self, *args, **kwargs) -> None:
        self._app.log_write(args[0] if args else "")

    def pager(self, **kwargs):
        return contextlib.nullcontext()


class TwoBApp(App):
    # Colors come from the active theme (see theme.py) via `$tb-*` CSS variables
    # (supplied by get_css_variables) and, for rich markup built in Python, via
    # self.c(role). `/theme` swaps the palette live. `system` uses transparent
    # backgrounds so the user's own terminal background shows through.
    CSS = """
    Screen { layout: vertical; background: $tb-ground; color: $tb-ink; }
    #header { height: auto; padding: 1 2 0 2; background: $tb-ground; color: $tb-ink; }
    #log { height: 1fr; padding: 0 2; background: $tb-logbg; color: $tb-ink; }
    #log Static { color: $tb-ink; }
    /* Distinct looks so tool activity never reads like the model's answer: */
    #log .reply { border-left: thick $tb-accent; padding-left: 1; margin: 1 0; color: $tb-ink; }
    #log .user { text-style: bold; margin-top: 1; color: $tb-ink; }
    #log .tool { color: $tb-accent; }
    #log .toolresult { color: $tb-dim; }
    #plan { height: auto; padding: 0 2; background: $tb-ground; color: $tb-ink; }
    #palette { height: auto; padding: 0 2; background: $tb-ground; color: $tb-ink; }
    #input { margin: 0 2; border: round $tb-accent; background: $tb-panelbg; color: $tb-ink; }
    #input:focus { border: round $tb-accent; }
    #mode { height: 1; padding: 0 2; background: $tb-ground; color: $tb-faint; }
    #status { height: 1; padding: 0 2; background: $tb-panelbg; color: $tb-dim; }
    """
    BINDINGS = [
        Binding("tab", "palette_accept", "complete", priority=True, show=False),
        Binding("down", "pal_down", "next suggestion", show=False),
        Binding("up", "pal_up", "prev suggestion", show=False),
        Binding("shift+tab", "cycle_mode", "cycle mode", priority=True, show=False),
        Binding("ctrl+b", "background", "background task", show=False),
        Binding("ctrl+y", "copy", "copy last reply", show=False),
        Binding("escape", "interrupt", "interrupt", show=False),
        Binding("ctrl+d", "quit", "quit", show=False),
    ]

    def __init__(self, model: str, auto_yes: bool, initial_task: str | None,
                 theme_name: str = theme.DEFAULT_THEME):
        self.theme_name = theme_name if theme_name in theme.THEMES else theme.DEFAULT_THEME
        super().__init__()
        self.session = Session(default_model=model, auto_yes=auto_yes, cwd=os.getcwd())
        self.registry = registry.build_registry()
        self._quit = False
        self._fg_target: str | None = None
        self._initial_task = initial_task
        # NOTE: do NOT override self.console — Textual owns it for rendering.
        self.ui = _LogConsole(self)               # provider-neutral output for commands.py
        self._stream_text = ""
        self._stream_widget = None                # the in-flow Static currently being streamed into
        self._confirm_open = False                # a ConfirmScreen modal is currently shown
        self._pal: list[tuple[str, str]] = []     # current command-palette matches
        self._pal_index = 0                       # highlighted match (↑/↓ navigation)
        self._pending_tool = None                 # (name, args) between a tool START and its RESULT
        self._tool_widgets: list = []             # this task's tool-action widgets (for └ on the last)
        self._last_reply = ""                     # most-recent model reply, for /copy

    # ---- theming ----
    def get_css_variables(self) -> dict[str, str]:
        base = super().get_css_variables()
        base.update(theme.css_variables(self.theme_name))
        return base

    def c(self, role: str) -> str:
        """Hex color for a semantic role in the active theme, for rich markup.
        For `transparent` grounds there's no meaningful text color — callers only
        ask for foreground roles (ink/accent/dim/faint/ok/err)."""
        return theme.colors(self.theme_name).get(role, "#8A7A45")

    def set_theme(self, name: str) -> bool:
        name = name.strip().lower()
        if name not in theme.THEMES:
            self.log_write(Text(f"Unknown theme '{name}'. Options: {', '.join(theme.THEMES)}.",
                                style=self.c("err")))
            return False
        self.theme_name = name
        self.refresh_css()                                   # re-apply CSS with the new $tb-* vars
        self.query_one("#header", Static).update(self._banner_header())
        self.log_write(Text(f"Theme: {name}", style=self.c("accent")))
        return True

    # ---- layout ----
    def compose(self) -> ComposeResult:
        yield Static(self._banner_header(), id="header")
        yield VerticalScroll(id="log")
        yield Static("", id="plan")
        yield Static("", id="palette")
        inp = Input(placeholder="Type a task, or / for commands", id="input")
        inp.border_title = "2B"
        yield inp
        yield Static("", id="mode")
        yield Static("", id="status")

    def on_mount(self) -> None:
        self.query_one("#input", Input).focus()
        self.set_interval(1 / 12, self._tick)
        for line in self._intro_lines():
            self.log_write(line)
        if self._initial_task:
            self._start_task(self._initial_task)

    # ---- helpers exposed to commands.py (duck interface) ----
    def request_quit(self) -> None:
        self._quit = True
        self.exit()

    def request_foreground(self, task_id: str) -> None:
        self._fg_target = task_id  # honored in _maybe_start_next on the next tick

    def enqueue_task(self, description: str):
        return self.session.add_task(description)

    def log_write(self, renderable, classes: str = "") -> None:
        log = self.query_one("#log", VerticalScroll)
        log.mount(Static(renderable, classes=classes or None))
        log.scroll_end(animate=False)

    # ---- input + command palette ----
    def on_input_changed(self, event: Input.Changed) -> None:
        self._pal_index = 0                        # a new filter resets the highlight
        self._update_palette(event.value)

    def action_palette_accept(self) -> None:
        """Tab: fill the highlighted match into the input (no run)."""
        self._accept_palette(run=False)

    def action_pal_down(self) -> None:
        if self._pal:
            self._pal_index = (self._pal_index + 1) % len(self._pal)
            self._render_palette()

    def action_pal_up(self) -> None:
        if self._pal:
            self._pal_index = (self._pal_index - 1) % len(self._pal)
            self._render_palette()

    def _palette_selected(self) -> str | None:
        if not self._pal:
            return None
        return self._pal[self._pal_index % len(self._pal)][0]

    def _clear_palette(self) -> None:
        self._pal = []
        self._pal_index = 0
        self.query_one("#palette", Static).update("")

    def _accept_palette(self, run: bool) -> None:
        """Take the highlighted match. `/model` fills so you can pick a model next;
        anything else runs when `run` (Enter), or fills when not (Tab)."""
        sel = self._palette_selected()
        if sel is None:
            return
        inp = self.query_one("#input", Input)
        if run and sel != "/model":               # /model opens a second-stage model menu
            self._clear_palette()
            inp.value = ""
            self._submit(sel)
            return
        inp.value = sel + " "                      # fill; on_input_changed recomputes the menu
        inp.cursor_position = len(inp.value)

    def _model_candidates(self) -> list[str]:
        out = []
        for pname, prov in registry.usable(self.registry).items():
            try:
                out += [f"{pname}:{m}" for m in prov.list_models()]
            except Exception:
                continue
        return out

    def _update_palette(self, text: str) -> None:
        """Recompute the matches for the current input, then render."""
        matches: list[tuple[str, str]] = []
        if text.startswith("/"):
            body = text[1:]
            if " " not in body:                            # completing the command name
                matches = [(f"/{n}", d) for n, d in command_specs() if n.startswith(body)]
            else:
                cmd, _, arg = body.partition(" ")
                if cmd == "model":                          # completing a model name
                    matches = [(f"/model {full}", "") for full in self._model_candidates()
                               if arg.lower() in full.lower()]
        self._pal = matches
        if self._pal_index >= len(matches):
            self._pal_index = 0
        self._render_palette()

    def _render_palette(self) -> None:
        pal = self.query_one("#palette", Static)
        matches = self._pal
        if not matches:
            pal.update("")
            return
        accent, dim, faint = self.c("accent"), self.c("dim"), self.c("faint")
        window = 8
        top = 0 if self._pal_index < window else self._pal_index - window + 1
        lines = []
        if top > 0:
            lines.append(f"  [{faint}]↑ +{top} more[/{faint}]")
        for i in range(top, min(top + window, len(matches))):
            txt, doc = matches[i]
            meta = f"  [{dim}]{doc}[/{dim}]" if doc else ""
            if i == self._pal_index:               # the ↑/↓ · enter target
                lines.append(f"[reverse {accent}] {txt} [/reverse {accent}]{meta}   [{faint}](↑↓ · enter)[/{faint}]")
            else:
                lines.append(f"  [{accent}]{txt}[/{accent}]{meta}")
        remaining = len(matches) - min(top + window, len(matches))
        if remaining > 0:
            lines.append(f"  [{faint}]↓ +{remaining} more[/{faint}]")
        pal.update("\n".join(lines))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._pal:                              # menu open → Enter selects the highlight
            self._accept_palette(run=True)
            return
        raw = event.value.strip()
        self.query_one("#input", Input).value = ""
        self._clear_palette()
        if not raw:
            return
        self._submit(raw)

    def _submit(self, raw: str) -> None:
        self.log_write(Text(f"› {raw}"), classes="user")
        if raw.startswith("/"):
            dispatch_input(raw, self)
            return
        self._start_task(raw)

    def _start_task(self, description: str) -> None:
        active = self.session.active_task
        if active is not None and active.state in (TaskState.ACTIVE, TaskState.BACKGROUNDED):
            self.enqueue_task(description)             # queue; will run when current finishes
            self.log_write(Text(f"  queued: {description[:60]}", style="dim"))
            return
        task = self.session.add_task(description)
        self._run(task)

    def _run(self, task) -> None:
        self.session.active_task_id = task.id
        task.state = TaskState.ACTIVE
        task.thread = threading.Thread(
            target=orchestrator.run_task,
            args=(self.session, task, self.session.events.put, self.registry),
            daemon=True,
        )
        task.thread.start()

    # ---- actions ----
    def action_background(self) -> None:
        t = self.session.active_task
        if t is not None and t.state == TaskState.ACTIVE:
            t.state = TaskState.BACKGROUNDED
            self.session.active_task_id = None
            self.log_write(Text(f"backgrounded [{t.id}] {t.title} — /fg {t.id} to resume", style="dim"))

    def action_interrupt(self) -> None:
        t = self.session.active_task
        if t is not None:
            t.cancel_flag.set()
            self.log_write(Text("interrupting…", style="dim"))

    def action_cycle_mode(self) -> None:
        self.session.cycle_mode()
        self._render_mode()

    def action_copy(self) -> None:
        self.copy_last()

    def copy_to_clipboard(self, text: str) -> None:
        """Prefer pbcopy on macOS (Terminal.app ignores OSC-52), else fall back to
        Textual's OSC-52 path. This also backs the built-in drag-to-select copy:
        selecting text with the mouse and pressing ctrl+c routes here too."""
        import subprocess
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            return
        except Exception:
            pass
        super().copy_to_clipboard(text)

    def copy_last(self) -> None:
        """Copy the last model reply to the clipboard (for /copy, ctrl+y)."""
        text = (self._last_reply or "").strip()
        if not text:
            self.log_write(Text("Nothing to copy yet.", style=self.c("faint")))
            return
        self.copy_to_clipboard(text)
        self.log_write(Text(f"Copied last reply to clipboard ({len(text)} chars).", style=self.c("accent")))

    def _render_mode(self) -> None:
        mode = self.session.mode
        glyph, color = _MODE_STYLE.get(mode, ("▷▷", self.c("faint")))
        t = Text()
        t.append(f"{glyph} ", style=color)
        t.append(f"{MODE_LABELS.get(mode, mode)} on", style=f"bold {color}")
        t.append("   (shift+tab to cycle)", style=self.c("faint"))
        self.query_one("#mode", Static).update(t)

    # ---- the periodic pump: drain events, render ----
    def _tick(self) -> None:
        self._drain_events()
        self._render_plan()
        self._render_mode()
        self._render_status()
        self._maybe_start_next()

    def _tool_line(self, connector: str, glyph: str, gstyle: str, phrase: str) -> Text:
        t = Text()
        t.append(f"  {connector} ", style=self.c("faint"))   # tree gutter
        t.append(f"{glyph} ", style=gstyle)                  # ✓ / ✗
        t.append(phrase, style=self.c("accent"))             # conversational action
        return t

    def _render_tool_action(self, result: str) -> None:
        name, args = self._pending_tool or ("", {})
        self._pending_tool = None
        phrase = _describe_tool(name, args)
        ok = not result.strip().startswith("error")
        glyph, gstyle = ("✓", self.c("ok")) if ok else ("✗", self.c("err"))
        log = self.query_one("#log", VerticalScroll)
        action = Static(self._tool_line("├", glyph, gstyle, phrase))
        log.mount(action)
        self._tool_widgets.append((action, glyph, gstyle, phrase))
        # concise dim result sub-line
        first = result.splitlines()[0] if result else ""
        if first:
            style = self.c("err") if not ok else self.c("faint")
            log.mount(Static(Text(f"      {first[:160]}", style=style)))
        log.scroll_end(animate=False)

    def _close_tool_group(self) -> None:
        # Turn the last action's connector into └ so the group reads as a tree.
        if self._tool_widgets:
            w, glyph, gstyle, phrase = self._tool_widgets[-1]
            w.update(self._tool_line("└", glyph, gstyle, phrase))
        self._tool_widgets = []

    def _commit_stream(self) -> None:
        # The reply streamed as plain text (fast, and partial Markdown renders
        # badly mid-stream). Now that it's complete, re-render it as Markdown so
        # headings, bold, code, and tables format properly.
        if self._stream_widget is not None and self._stream_text.strip():
            self._last_reply = self._stream_text
            try:
                self._stream_widget.update(Markdown(self._stream_text))
            except Exception:
                self._stream_widget.update(Text(self._stream_text))
        self._stream_widget = None
        self._stream_text = ""

    def _drain_events(self) -> None:
        q = self.session.events
        log = self.query_one("#log", VerticalScroll)
        while not q.empty():
            ev = q.get()
            t = ev.type
            if t == EventType.ASSISTANT_DELTA:
                if self._stream_widget is None:
                    self._stream_widget = Static(Text(""), classes="reply")
                    log.mount(self._stream_widget)
                self._stream_text += ev.payload["chunk"]
                self._stream_widget.update(Text(self._stream_text))
                log.scroll_end(animate=False)
            elif t == EventType.TURN_START:
                self._commit_stream()
            elif t == EventType.TOOL_CALL_START:
                self._commit_stream()
                self._pending_tool = (ev.payload["name"], ev.payload["shown"])
            elif t == EventType.TOOL_CALL_RESULT:
                self._render_tool_action(ev.payload["result"])
            elif t == EventType.ASSISTANT_TEXT:
                self._commit_stream()
                self._close_tool_group()
                self._last_reply = ev.payload["text"]
                self.log_write(Markdown(ev.payload["text"]), classes="reply")
            elif t == EventType.LOG:
                self._commit_stream()
                self.log_write(Text(f"✻ {ev.payload.get('text', '')}", style=self.c("dim")))
            elif t == EventType.TASK_ERROR:
                self._commit_stream()
                self._close_tool_group()
                self.log_write(Text(f"error: {ev.payload.get('error', 'unknown')}", style=f"bold {self.c('err')}"))
            elif t == EventType.TASK_DONE:
                self._commit_stream()
                self._close_tool_group()

        # write confirmation waiting? show it as a modal.
        active = self.session.active_task
        if not self._confirm_open and active is not None and active.pending is not None:
            self._confirm_open = True
            pc = active.pending

            def _resolved(approved, _pc=pc):
                _pc.approved = bool(approved)
                _pc.answered.set()
                self._confirm_open = False

            self.push_screen(ConfirmScreen(pc.prompt, pc.diff), _resolved)

    def _maybe_start_next(self) -> None:
        # explicit /fg foregrounding request takes priority
        if self._fg_target:
            t = self.session.find(self._fg_target)
            self._fg_target = None
            if t is not None:
                if t.state == TaskState.BACKGROUNDED:
                    t.state = TaskState.ACTIVE
                    self.session.active_task_id = t.id
                    self.log_write(Text(f"foregrounded [{t.id}] {t.title}", style=self.c("dim")))
                    return
                if t.state == TaskState.QUEUED and self.session.active_task is None:
                    self._run(t)
                    return
        if self.session.active_task is not None:
            return
        nxt = next((t for t in self.session.tasks if t.state == TaskState.QUEUED), None)
        if nxt is not None:
            self._run(nxt)

    def _render_plan(self) -> None:
        task = self.session.active_task
        plan = self.query_one("#plan", Static)
        if task is None or not task.plan_steps:
            plan.update("")
            return
        style_of = {
            "done": ("✓", self.c("ok")),
            "active": ("■", f"bold {self.c('ink')}"),
            "pending": ("□", self.c("faint")),
        }
        lines = []
        for s in task.plan_steps[:VISIBLE_STEPS]:
            glyph, style = style_of[s.status]
            lines.append(f"[{style}]{glyph} {s.text}[/{style}]")
        hidden = task.plan_steps[VISIBLE_STEPS:]
        if hidden:
            pend = sum(1 for s in hidden if s.status == "pending")
            done = sum(1 for s in hidden if s.status == "done")
            faint = self.c("faint")
            lines.append(f"[{faint} italic]… +{pend} pending, {done} completed[/{faint} italic]")
        plan.update("\n".join(lines))


    def _render_status(self) -> None:
        st = self.query_one("#status", Static)
        task = self.session.active_task
        model = self.session.default_model
        if task is not None and task.state == TaskState.ACTIVE and task.status_line:
            frame = _SPIN[int(time.monotonic() * 12) % len(_SPIN)]
            elapsed = int(time.monotonic() - task.turn_started_at) if task.turn_started_at else 0
            perf = f"  ·  {task.perf}" if task.perf else ""
            left = f"{frame} {task.status_line}… ({elapsed}s){perf}"
        else:
            left = "idle"
        others = [t for t in self.session.tasks if t.id != self.session.active_task_id
                  and t.state in (TaskState.QUEUED, TaskState.BACKGROUNDED)]
        extra = f"  ·  {len(others)} queued/bg" if others else ""
        st.update(f"{left}    │    {model}{extra}    ·  esc interrupt · ctrl+b bg · ctrl+d quit")

    # ---- header / intro ----
    def _banner_header(self) -> Text:
        from . import __version__
        home = os.path.expanduser("~")
        cwd = self.session.cwd
        path = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
        t = Text()
        t.append("2B", style=f"bold {self.c('ink')}")
        t.append(f"  v{__version__}   ", style=self.c("dim"))
        t.append(self.session.default_model, style=self.c("accent"))
        t.append("  ·  local  ·  Ollama\n", style=self.c("dim"))
        t.append(path, style=self.c("dim"))
        return t

    def _intro_lines(self) -> list:
        return [
            Text("Local models, kept on task.", style="bold"),
            Text("Type a task to begin, or / for commands.", style="dim"),
            Text(""),
        ]


def run_tui(model: str, auto_yes: bool, initial_task: str | None,
            theme_name: str = theme.DEFAULT_THEME) -> None:
    TwoBApp(model, auto_yes, initial_task, theme_name).run()
