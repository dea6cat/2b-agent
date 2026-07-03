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

from . import completion, config, difffmt, orchestrator, registry, theme, tools
from .commands import command_specs, dispatch_input
from .orchestrator import EventType
from .session import MODE_ACCEPT, MODE_LABELS, MODE_PLAN, Session, TaskState
from .tui import VISIBLE_STEPS

# Mode indicator glyph + accent color (fixed hues that read on every theme).
_MODE_STYLE = {
    MODE_ACCEPT: ("▶▶", "#A78BD0"),   # accept edits — purple
    MODE_PLAN: ("❚❚", "#5FA69C"),     # plan mode — teal
}


_DIFF_ADD_BG = "on #12331a"   # dark green — added lines
_DIFF_DEL_BG = "on #3a1519"   # dark red   — removed lines


def render_diff(diff: str) -> Text:
    """Render a unified diff inline, Claude-Code style: a `+N -M` summary, then each
    line numbered with a green/red background for added/removed and dim for context.
    A non-diff preview (e.g. a whole-file overwrite note) renders plainly."""
    diff = diff or "(no preview)"
    t = Text()
    if not difffmt.is_unified_diff(diff):
        for line in diff.splitlines():
            t.append(line + "\n", style="dim")
        return t
    add, rem = difffmt.diff_counts(diff)
    t.append(f"  +{add} -{rem}\n", style="dim")
    for old_no, new_no, kind, text in difffmt.diff_rows(diff):
        if kind == "add":
            t.append(f"{new_no:>5} + {text}\n", style=_DIFF_ADD_BG)
        elif kind == "del":
            t.append(f"{old_no:>5} - {text}\n", style=_DIFF_DEL_BG)
        else:
            t.append(f"{new_no:>5}   {text}\n", style="dim")
    return t


class ConnectScreen(ModalScreen[str | None]):
    """Masked prompt for a provider API key, so it never lands in the log.
    Returns the entered key, or None if cancelled."""
    CSS = """
    ConnectScreen { align: center middle; }
    #box { width: 80%; max-width: 90; height: auto; border: round #8A7A45;
           background: #C7C1AE; color: #454235; padding: 1 2; }
    #btns { height: auto; padding-top: 1; align-horizontal: right; }
    Button { margin-left: 2; }
    """

    def __init__(self, provider: str):
        super().__init__()
        self._provider = provider

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(f"Paste your {self._provider} API key (hidden), then Enter:")
            yield Input(password=True, id="key")
            with Horizontal(id="btns"):
                yield Button("Connect", variant="success", id="ok")
                yield Button("Cancel", variant="error", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#key", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        key = self.query_one("#key", Input).value.strip()
        self.dismiss(key if event.button.id == "ok" and key else None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


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
    if name == "run_git":
        return f"Running git {args.get('args', '')}".strip()
    if name == "run_command":
        return f"Running {args.get('command', '')}".strip()
    if "__" in name:                       # MCP tool: server__tool
        server, _, tool = name.partition("__")
        return f"{server} · {tool}"
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
                 theme_name: str = theme.DEFAULT_THEME, resume_conv=None, resume_id=None):
        self.theme_name = theme_name if theme_name in theme.THEMES else theme.DEFAULT_THEME
        super().__init__()
        self.session = Session(default_model=model, auto_yes=auto_yes, cwd=os.getcwd())
        self.registry = registry.build_registry()
        self._quit = False
        self._fg_target: str | None = None
        self._initial_task = initial_task
        self._resume_conv = resume_conv          # attached to the first task created (--continue/--resume)
        self._resume_id = resume_id              # …which also adopts this id so its save updates that row
        # NOTE: do NOT override self.console — Textual owns it for rendering.
        self.ui = _LogConsole(self)               # provider-neutral output for commands.py
        self._stream_text = ""
        self._stream_widget = None                # the in-flow Static currently being streamed into
        self._pending_confirm = None              # PendingConfirmation shown inline (answered y/n in view)
        self._default_placeholder = "Type a task, or / for commands"
        self._pal: list[tuple[str, str]] = []     # current command-palette matches
        self._pal_index = 0                       # highlighted match (↑/↓ navigation)
        self._pal_mode = ""                       # "cmd" (slash) or "file" (@) — how to accept
        self._file_list = None                    # cached project relpaths for @-completion
        self._pending_tool = None                 # (name, args) between a tool START and its RESULT
        self._tool_widgets: list = []             # this task's tool-action widgets (for └ on the last)
        self._last_reply = ""                     # most-recent model reply, for /copy
        self._ctx_label = ""                      # "13k ctx" for the banner (filled async)
        self._ctx_budget = 0                      # token window for the live context meter (filled async)
        self._ctx_cache = (None, ("", ))          # ((conv id, msg count) -> rendered meter segment)

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
        inp.border_title = "2B Agent"
        yield inp
        yield Static("", id="mode")
        yield Static("", id="status")

    def on_mount(self) -> None:
        self.query_one("#input", Input).focus()
        self.set_interval(1 / 12, self._tick)
        for line in self._intro_lines():
            self.log_write(line)
        # Resolve the model's context window off-thread so a slow /api/show never
        # blocks startup; refresh the banner once it's known.
        threading.Thread(target=self._load_ctx_label, daemon=True).start()
        if self._initial_task:
            self._start_task(self._initial_task)

    def _load_ctx_label(self) -> None:
        try:
            resolved = registry.resolve(self.registry, self.session.default_model)
            if not resolved:
                return
            provider, model = resolved
            # Budget for the live context meter — works for local (pinned num_ctx) and
            # cloud (per-provider budget). Resolved once here, off the render path.
            self._ctx_budget = orchestrator.context_budget(provider, model)
            is_local = getattr(provider, "name", "") == "ollama" and getattr(provider, "api_key", None) is None
            if not is_local:
                return   # banner "Nk ctx" label is only meaningful for the num_ctx 2B pins locally
            win = self._ctx_budget
        except Exception:
            return
        if win:
            self._ctx_label = f"{win // 1000}k ctx" if win >= 1000 else f"{win} ctx"
            self.call_from_thread(lambda: self.query_one("#header", Static).update(self._banner_header()))

    # ---- helpers exposed to commands.py (duck interface) ----
    def request_quit(self) -> None:
        self._quit = True
        self.exit()

    def request_foreground(self, task_id: str) -> None:
        self._fg_target = task_id  # honored in _maybe_start_next on the next tick

    def enqueue_task(self, description: str):
        task = self.session.add_task(description)
        if self._resume_conv is not None:        # first task adopts the resumed thread + its id
            task.conversation = self._resume_conv
            if self._resume_id:
                task.id = self._resume_id
            self._resume_conv = None
            self._resume_id = None
        return task

    def begin_connect(self, provider: str) -> None:
        """Collect a provider key in a masked modal, then save + re-detect."""
        def _done(key, _p=provider):
            if not key:
                self.log_write(Text(f"Cancelled connecting {_p}.", style=self.c("faint")))
                return
            config.connect(_p, key)
            self.registry = registry.build_registry()
            self.log_write(Text(f"Connected {_p} ({config.mask(key)}). Saved for future sessions.",
                                style=self.c("accent")))
        self.push_screen(ConnectScreen(provider), _done)

    def log_write(self, renderable, classes: str = "") -> None:
        log = self.query_one("#log", VerticalScroll)
        log.mount(Static(renderable, classes=classes or None))
        log.scroll_end(animate=False)

    def clear_screen(self) -> None:
        """Wipe the conversation log back to a fresh-session state (for /clear)."""
        self._commit_stream()
        self.query_one("#log", VerticalScroll).remove_children()
        self._stream_widget = None
        self._stream_text = ""
        self._pending_tool = None
        self._tool_widgets = []
        self._last_reply = ""
        for line in self._intro_lines():
            self.log_write(line)

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
        self._pal_mode = ""
        self.query_one("#palette", Static).update("")

    def _accept_palette(self, run: bool) -> None:
        """Take the highlighted match. `/model` fills so you can pick a model next;
        anything else runs when `run` (Enter), or fills when not (Tab)."""
        sel = self._palette_selected()
        if sel is None:
            return
        inp = self.query_one("#input", Input)
        if self._pal_mode == "file":               # replace the trailing '@partial' with the path
            text = inp.value
            idx = text.rfind("@")
            if idx != -1:
                inp.value = text[:idx] + sel + " "
                inp.cursor_position = len(inp.value)
            self._clear_palette()
            return
        # These open a second-stage menu (model / provider), so fill rather than run.
        opens_submenu = sel in ("/model", "/connect", "/login", "/disconnect")
        if run and not opens_submenu:
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

    def _project_files(self):
        """Cached list of project relpaths (shortest first) for @-completion. Skips
        the usual junk dirs; capped so a huge repo can't stall the first completion."""
        if self._file_list is None:
            root, out = self.session.cwd, []
            for dp, dns, fns in os.walk(root):
                dns[:] = [d for d in dns if not tools._should_skip_dir(d)]
                for fn in fns:
                    if fn.startswith("."):
                        continue
                    out.append(os.path.relpath(os.path.join(dp, fn), root))
                    if len(out) >= 4000:
                        break
                if len(out) >= 4000:
                    break
            out.sort(key=len)
            self._file_list = out
        return self._file_list

    def _update_palette(self, text: str) -> None:
        """Recompute the matches for the current input, then render. Slash completes
        commands/models/providers; an @-token completes project file paths."""
        matches: list[tuple[str, str]] = []
        mode = ""
        if text.startswith("/"):
            mode = "cmd"
            body = text[1:]
            if " " not in body:                            # completing the command name
                matches = [(f"/{n}", d) for n, d in command_specs() if n.startswith(body)]
            else:
                cmd, _, arg = body.partition(" ")
                if cmd in ("model", "default"):             # completing a model name
                    matches = [(f"/{cmd} {full}", "") for full in self._model_candidates()
                               if arg.lower() in full.lower()]
                elif cmd in ("connect", "login", "disconnect"):   # completing a provider
                    matches = [(f"/{cmd} {p}", "connected" if config.is_connected(p) else "")
                               for p in config.PROVIDER_KEY_ENV if p.startswith(arg.lower())]
        else:
            tok = completion.at_token(text)                # typing '@path' -> file completion
            if tok is not None:
                mode = "file"
                matches = [(f, "") for f in completion.rank_files(self._project_files(), tok)]
        self._pal, self._pal_mode = matches, mode
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

    @staticmethod
    def _echo_safe(raw: str) -> str:
        # Never echo an inline API key (/connect <provider> <key>) into the log.
        parts = raw.split()
        if len(parts) >= 3 and parts[0] in ("/connect", "/login"):
            return f"{parts[0]} {parts[1]} ••••••"
        return raw

    def _submit(self, raw: str) -> None:
        self.log_write(Text(f"› {self._echo_safe(raw)}"), classes="user")
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
        task = self.enqueue_task(description)
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
        if t is not None and t.state == TaskState.ACTIVE:
            t.cancel_flag.set()                 # orchestrator aborts the stream; subprocess tools killpg within ~100ms
            # Tear down the long-lived helpers (LSP/MCP) off the UI thread so a slow
            # server can't freeze the interface while we stop everything.
            threading.Thread(target=orchestrator.teardown_helpers, daemon=True).start()
            self.log_write(Text("stopping…", style=self.c("faint")))

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
        # concise dim result sub-line — errors get more room so recovery guidance
        # (e.g. the run_git "no shell operators" message) isn't clipped mid-word.
        first = result.splitlines()[0] if result else ""
        if first:
            style = self.c("err") if not ok else self.c("faint")
            log.mount(Static(Text(f"      {first[:400 if not ok else 160]}", style=style)))
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

        # Write/edit confirmation waiting? Show it INLINE in the conversation (diff +
        # y/n), never a popup. Answered by _resolve_confirm on a keypress.
        active = self.session.active_task
        pc = active.pending if active is not None else None
        if pc is not None and self._pending_confirm is not pc:
            self._pending_confirm = pc
            self._show_inline_confirm(pc)
        elif pc is None and self._pending_confirm is not None:
            # the worker cleared it out from under us (e.g. esc cancelled the task) —
            # reset the inline state so the input works again.
            self._pending_confirm = None
            self._restore_input()

    def _show_inline_confirm(self, pc) -> None:
        """Render the proposed change inline in the view and arm y/n. No modal."""
        self._commit_stream()
        self.log_write(render_diff(pc.diff))
        q = Text(f"{pc.prompt}  ", style=f"bold {self.c('ink')}")
        q.append("y", style="green")
        q.append(" apply · ", style=self.c("dim"))
        q.append("n", style="red")
        q.append(" skip · ", style=self.c("dim"))
        q.append("esc", style=self.c("dim"))
        q.append(" stop", style=self.c("dim"))
        self.log_write(q)
        inp = self.query_one("#input", Input)
        inp.placeholder = "y apply · n skip · esc stop"
        inp.disabled = True

    def _resolve_confirm(self, approved: bool) -> None:
        pc = self._pending_confirm
        self._pending_confirm = None
        if pc is not None:
            pc.approved = bool(approved)
            pc.answered.set()
        self.log_write(Text("  ✔ applied" if approved else "  ✗ skipped", style=self.c("dim")))
        self._restore_input()

    def _restore_input(self) -> None:
        inp = self.query_one("#input", Input)
        inp.disabled = False
        inp.placeholder = self._default_placeholder
        inp.focus()

    def on_key(self, event) -> None:
        # While an inline confirmation is armed, y/enter apply and n skips; the keys
        # are consumed so they don't leak into the input. esc is left to the normal
        # interrupt binding (which cancels the whole task via the worker's cancel check).
        if self._pending_confirm is None:
            return
        if event.key in ("y", "Y", "enter"):
            event.stop()
            self._resolve_confirm(True)
        elif event.key in ("n", "N"):
            event.stop()
            self._resolve_confirm(False)

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
        st.update(f"{left}    │    {model}{extra}{self._ctx_meter(task)}    ·  esc stop · ctrl+b bg · ctrl+d quit")

    def _ctx_meter(self, task) -> str:
        """Live context-window fill (small local windows fill fast — this is the point).
        Amber past 80%. Empty when there's no conversation or the budget isn't known yet.
        estimate_tokens is O(conversation) and this renders ~12x/s, so the result is
        memoized and only recomputed when a message is added (keyed on conv id + count)."""
        if task is None or task.conversation is None or self._ctx_budget <= 0:
            return ""
        conv = task.conversation
        key = (id(conv), len(conv.messages), self._ctx_budget)
        if self._ctx_cache[0] != key:
            pct, warn = orchestrator.context_usage(orchestrator.estimate_tokens(conv), self._ctx_budget)
            seg = f"  ·  [yellow]ctx {pct}%[/yellow]" if warn else f"  ·  ctx {pct}%"
            self._ctx_cache = (key, (seg,))
        return self._ctx_cache[1][0]

    def on_model_changed(self) -> None:
        """Called by /model and /default after a switch: recompute the context budget
        (and banner label) for the new model, off the render path, and drop the meter
        cache so it re-measures against the new window."""
        self._ctx_cache = (None, ("",))
        threading.Thread(target=self._load_ctx_label, daemon=True).start()

    # ---- header / intro ----
    def _banner_header(self) -> Text:
        from . import __version__
        home = os.path.expanduser("~")
        cwd = self.session.cwd
        path = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
        t = Text()
        t.append("2B Agent", style=f"bold {self.c('ink')}")
        t.append(f"  v{__version__}   ", style=self.c("dim"))
        t.append(self.session.default_model, style=self.c("accent"))
        if self._ctx_label:
            t.append(f"  ·  {self._ctx_label}", style=self.c("dim"))
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
            theme_name: str = theme.DEFAULT_THEME, resume_conv=None, resume_id=None) -> None:
    TwoBApp(model, auto_yes, initial_task, theme_name,
            resume_conv=resume_conv, resume_id=resume_id).run()
