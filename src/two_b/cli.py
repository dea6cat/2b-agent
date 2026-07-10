"""2B command-line entry point and REPL loop (Milestone 2).

Runs a persistent session: reads input, dispatches slash commands, and drives
tasks. The App is the single owner of the terminal — worker threads never write
stdout (see orchestrator.py). The rich.Live region always fully exits before any
input() prompt, so confirmation prompts never fight the live display.
"""
import argparse
import os
import sys
import threading
import time

from rich.console import Console

from . import __version__, banner, orchestrator, registry, tui
from .commands import dispatch_input
from .prompt import make_session, prompt_line
from .rawkey import CTRL_B, KeyListener
from .orchestrator import AgentEvent, EventType
from .session import Session, Task, TaskState
from .tui import render_session


class App:
    def __init__(self, model: str, auto_yes: bool, resume_conv=None, resume_id=None):
        # Pin the Console to the real stdout at startup so a worker thread's
        # redirect_stdout (used to capture verbatim-tool print output) can never
        # divert the UI's terminal writes.
        self.console = Console(file=sys.stdout)
        self.ui = self.console   # provider-neutral output handle used by commands.py
        self.session = Session(default_model=model, auto_yes=auto_yes, cwd=os.getcwd())
        self.registry = registry.build_registry()
        self.listener = KeyListener(on_key=self._on_key)
        self._session = make_session(self)
        self._quit = False
        self._background_requested = False
        self._fg_target: str | None = None
        self._resume_conv = resume_conv   # attached to the first task created (--continue/--resume)
        self._resume_id = resume_id       # …which also adopts this id so its save updates that row

    # --- callbacks used by commands.py (duck-typed App interface) ---
    def request_quit(self) -> None:
        self._quit = True

    def request_foreground(self, task_id: str) -> None:
        self._fg_target = task_id

    def on_model_changed(self) -> None:
        """Hook for /model and /default; the line-mode REPL has no context meter to refresh."""
        pass

    def enqueue_task(self, description: str) -> Task:
        task = self.session.add_task(description)
        if self._resume_conv is not None:        # first task adopts the resumed thread + its id
            task.conversation = self._resume_conv
            if self._resume_id:
                task.id = self._resume_id
            from . import changelog       # restore the durable undo stack for this thread
            task.edit_history = changelog.load(task.id, self.session.cwd)
            self._resume_conv = None
            self._resume_id = None
        return task

    # --- key handling ---
    def _on_key(self, ch: str) -> None:
        if ch == CTRL_B:
            self._background_requested = True

    # --- event rendering (UI thread only) ---
    def _print_event(self, ev: AgentEvent) -> None:
        if ev.type == EventType.TOOL_CALL_START:
            self.console.print(f"[cyan]→ {ev.payload['name']} {ev.payload['shown']}[/cyan]")
        elif ev.type == EventType.TOOL_CALL_RESULT:
            result = ev.payload["result"]
            first = result.splitlines()[0] if result else ""
            style = "red" if first.startswith("error") else "dim"
            self.console.print(f"  [{style}]{first[:200]}[/{style}]")
        elif ev.type == EventType.LOG:
            self.console.print(f"[dim]{ev.payload.get('text', '')}[/dim]")
        elif ev.type == EventType.ASSISTANT_TEXT:
            self.console.print()
            self.console.print(ev.payload["text"])
        elif ev.type == EventType.TASK_ERROR:
            self.console.print(f"[bold red]error: {ev.payload.get('error', 'unknown')}[/bold red]")

    # --- the watch loop for the foregrounded task ---
    def _watch(self, task: Task) -> None:
        self.session.active_task_id = task.id
        if task.state == TaskState.QUEUED:
            task.state = TaskState.ACTIVE
            task.thread = threading.Thread(
                target=orchestrator.run_task,
                args=(self.session, task, self.session.events.put, self.registry),
                daemon=True,
            )
            task.thread.start()
        elif task.state == TaskState.BACKGROUNDED:
            task.state = TaskState.ACTIVE
        self._watch_loop(task)

    def _watch_loop(self, task: Task) -> None:
        from rich.live import Live

        # Live spinner+checklist is shown while WAITING (thinking / running a
        # tool). When the model starts streaming its reply, the Live region is
        # stopped and tokens are written inline; the line is closed before any
        # tool output or completion. The key listener (raw mode) runs only here,
        # never competing with the idle prompt.
        self._background_requested = False
        self.listener.start()
        live = None
        mid_stream = False
        thinking_started = None

        def stop_live():
            nonlocal live
            if live is not None:
                live.stop()
                live = None

        def close_stream():
            nonlocal mid_stream
            if mid_stream:
                self.console.print()   # end the streamed paragraph
                mid_stream = False

        def close_thinking():
            # End the dim streamed reasoning line without a collapsed summary
            # (no reply followed it — e.g. a thinking-only turn, or a boundary
            # like TURN_START/TASK_DONE/an error). The reasoning text already
            # printed is left as-is; only the run state is reset.
            nonlocal thinking_started
            if thinking_started is not None:
                self.console.print()
                thinking_started = None

        q = self.session.events
        try:
            while True:
                while not q.empty():
                    ev = q.get()
                    if ev.type == EventType.THINKING_DELTA:
                        stop_live()
                        if thinking_started is None:
                            thinking_started = time.monotonic()
                        self.console.print(ev.payload["chunk"], end="",
                                           markup=False, highlight=False, soft_wrap=True, style="dim")
                    elif ev.type == EventType.ASSISTANT_DELTA:
                        stop_live()
                        if thinking_started is not None:
                            elapsed = time.monotonic() - thinking_started
                            thinking_started = None
                            self.console.print()
                            self.console.print(tui.thinking_summary(elapsed), style="dim")
                        if not mid_stream:
                            self.console.print()   # blank line before the reply
                            mid_stream = True
                        self.console.print(ev.payload["chunk"], end="",
                                           markup=False, highlight=False, soft_wrap=True)
                    elif ev.type == EventType.TURN_START:
                        close_stream()
                        close_thinking()
                    elif ev.type in (EventType.TOOL_CALL_START, EventType.TOOL_CALL_RESULT,
                                     EventType.LOG, EventType.ASSISTANT_TEXT, EventType.TASK_ERROR):
                        close_stream()
                        close_thinking()
                        stop_live()
                        self._print_event(ev)
                    elif ev.type == EventType.TASK_DONE:
                        close_stream()
                        close_thinking()

                if task.pending is not None:
                    close_stream()
                    close_thinking()
                    stop_live()
                    self._prompt_confirmation(task)
                    continue
                if self._background_requested:
                    self._background_requested = False
                    close_stream()
                    close_thinking()
                    stop_live()
                    task.state = TaskState.BACKGROUNDED
                    self.console.print(
                        f"[dim]Backgrounded [{task.id}] {task.title}. /tasks to check, /fg {task.id} to resume.[/dim]"
                    )
                    self.session.active_task_id = None
                    return
                if (task.thread is None or not task.thread.is_alive()) and q.empty():
                    close_stream()
                    close_thinking()
                    stop_live()
                    self.session.active_task_id = None
                    return

                if not mid_stream:
                    if live is None:
                        live = Live(render_session(self.session), console=self.console,
                                    refresh_per_second=12, transient=True)
                        live.start()
                    else:
                        live.update(render_session(self.session))
                time.sleep(0.05)
        finally:
            close_stream()
            close_thinking()
            stop_live()
            self.listener.stop()

    def _prompt_confirmation(self, task: Task) -> None:
        pc = task.pending
        if pc is None:
            return
        self.console.print(pc.diff)
        with self.listener.paused():
            try:
                ans = input(f"{pc.prompt} [y/N] ").strip().lower()
            except EOFError:
                ans = ""
        pc.approved = ans == "y"
        pc.answered.set()

    # --- runnable-task scheduling ---
    def _run_pending(self) -> None:
        if self._fg_target:
            target = self.session.find(self._fg_target)
            self._fg_target = None
            if target is not None and target.state in (TaskState.BACKGROUNDED, TaskState.ACTIVE):
                self._watch_loop(target)
                return
        nxt = next((t for t in self.session.tasks if t.state == TaskState.QUEUED), None)
        if nxt is not None:
            self._watch(nxt)

    def run(self, initial_task: str | None) -> None:
        # NOTE: the key listener is intentionally NOT started here. It runs only
        # inside _watch_loop while a task is active; at the idle prompt below the
        # terminal stays in normal canonical mode so input() works.
        try:
            # Startup banner — interactive sessions only (self._session is None
            # when stdin isn't a TTY), so scripted/piped runs stay clean.
            if self._session is not None:
                banner.render(self.console, self.session.default_model, self.session.cwd)
            if initial_task:
                self.enqueue_task(initial_task)
            self._run_pending()
            while not self._quit:
                try:
                    raw = prompt_line(self._session, self.session.default_model).strip()
                except EOFError:
                    self.console.print()
                    break
                except KeyboardInterrupt:
                    # ctrl+C at the idle prompt: clear the line, stay in the REPL.
                    continue
                if not raw:
                    self._run_pending()
                    continue
                if not dispatch_input(raw, self):
                    self.enqueue_task(raw)
                self._run_pending()
        finally:
            for t in self.session.tasks:
                t.cancel_flag.set()
            self.listener.stop()


def main() -> None:
    # `2b setup [flags]` — first-time onboarding. Intercepted before the main parser so
    # setup's own flags (--clean/--models/--no-benchmark/--fix-path) don't need declaring here.
    if sys.argv[1:2] == ["setup"]:
        from . import setup
        raise SystemExit(setup.main(sys.argv[2:]))

    # `2b eval …` — host-side technique scorer (drives the real agent over a fixed
    # task set). Intercepted before the parser so its own flags pass through. Gated on
    # a following flag (or nothing) so a free-text task like `2b eval this diff` — where
    # "eval" is just the first word — still runs as a task rather than being swallowed.
    if sys.argv[1:2] == ["eval"] and (len(sys.argv) == 2 or sys.argv[2].startswith("-")):
        from . import evals
        raise SystemExit(evals.main(sys.argv[2:]))

    # `2b trace replay <session>` — prompt-drift replay (P10). No LLM; rebuilds the recorded
    # prefix with current code and reports drift. Intercepted before the parser like eval/setup.
    if sys.argv[1:2] == ["trace"]:
        from . import driftreplay
        raise SystemExit(driftreplay.trace_main(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        prog="2b",
        description="A local-first coding agent that keeps small models focused.",
    )
    parser.add_argument("--model", help="Ollama model tag, e.g. qwen3.5:9b (default: autodetect)")
    parser.add_argument("--yes", action="store_true", help="Auto-apply file writes/edits without confirmation")
    parser.add_argument("--list-models", action="store_true", help="List available models across configured providers and exit")
    parser.add_argument("--classic", action="store_true", help="Use the line-mode REPL instead of the full-screen TUI")
    parser.add_argument("--theme", choices=["system", "light", "dark"], default="system",
                        help="TUI color theme (default: system — uses your terminal background)")
    parser.add_argument("--version", action="version", version=f"2b {__version__}")
    parser.add_argument("--print-ctx", metavar="MODEL", nargs="?", const="",
                        help="Print the context window 2B will run a local model at (sized to this machine), and exit")
    parser.add_argument("--doctor", action="store_true",
                        help="Run environment diagnostics (PATH, Ollama, default model) and exit")
    parser.add_argument("--rm", action="store_true",
                        help="Uninstall 2B and delete its config (~/.config/2b), then exit")
    parser.add_argument("--update", action="store_true",
                        help="Upgrade 2B to the latest release (uv tool upgrade) and exit")
    parser.add_argument("--setup", action="store_true",
                        help="Run first-time setup (Ollama, model download, PATH) and exit")
    parser.add_argument("--test", metavar="MODEL", nargs="?", const="",
                        help="Grade installed local models (tok/s + coding test) and exit. "
                             "Pass a model to test just one, or 'auto' to remove failing models.")
    parser.add_argument("--continue", dest="cont", action="store_true",
                        help="Resume the most recent session in this directory")
    parser.add_argument("--resume", metavar="ID", help="Resume a saved session by id (see --list-sessions)")
    parser.add_argument("--list-sessions", action="store_true",
                        help="List saved sessions for this directory and exit")
    parser.add_argument("task", nargs="?", help="An initial task to run before dropping into the session")
    args = parser.parse_args()

    # Load any provider keys saved via /connect before we detect providers.
    from . import config
    config.load_into_env()

    console = Console()

    if args.list_sessions:
        from . import persist
        rows = persist.list_sessions(cwd=os.getcwd())
        if not rows:
            console.print("[dim]No saved sessions for this directory.[/dim]")
        for r in rows:
            age = persist.relative_age(r["updated_at"]) if r.get("updated_at") else ""
            model = (r.get("model") or "").split(":")[-1]
            size = f"{r['messages']} msgs" if r.get("messages") else ""
            meta = "  ·  ".join(x for x in (age, model, size) if x)
            console.print(f"[cyan]{r['id']}[/cyan]  {r['title'] or '(untitled)'}"
                          + (f"   [dim]{meta}[/dim]" if meta else ""))
        if rows:
            console.print("[dim]Resume: [/dim][cyan]2b --resume <id>[/cyan][dim]  ·  latest: [/dim][cyan]2b --continue[/cyan]")
        raise SystemExit(0)

    if args.doctor:
        from . import doctor
        raise SystemExit(doctor.run(console.print))

    if args.rm:
        from . import uninstall

        def _confirm(prompt: str) -> bool:
            try:
                return input(f"{prompt} [y/N] ").strip().lower() == "y"
            except EOFError:
                return False
        raise SystemExit(uninstall.run(console.print, _confirm, args.yes))

    if args.update:
        from . import update
        raise SystemExit(update.run_upgrade(console.print))

    if args.setup:
        from . import setup
        raise SystemExit(setup.run({}))

    if args.test is not None:
        from . import testcmd

        def _confirm(prompt: str) -> bool:
            if not sys.stdin.isatty():        # never auto-delete without a TTY; require --yes
                return False
            try:
                return input(f"{prompt} [Y/n] ").strip().lower() != "n"
            except EOFError:
                return False
        auto = args.test == "auto"
        target = "" if auto else args.test
        raise SystemExit(testcmd.run(console.print, target=target, auto=auto,
                                     confirm=_confirm, assume_yes=args.yes))

    if args.list_models:
        from . import registry
        reg = registry.usable(registry.build_registry())
        if not reg:
            console.print("[red]No providers configured. Start Ollama, or set a provider API key.[/red]")
            raise SystemExit(1)
        for pname, prov in reg.items():
            try:
                models = prov.list_models()
            except Exception as e:
                console.print(f"[red]{pname}: {e}[/red]")
                continue
            for m in models:
                marker = "  (default)" if pname == "ollama" and m == orchestrator.DEFAULT_MODEL else ""
                console.print(f"{pname}:{m}{marker}")
        raise SystemExit(0)

    if args.print_ctx is not None:
        from . import catalog, registry
        m = args.print_ctx or args.model or orchestrator.pick_default_model()
        # Ollama-first, matching orchestrator.context_budget: a locally-pulled
        # model whose name collides with a cloud catalog entry (codestral,
        # devstral, …) must report its real pinned num_ctx, not cloud numbers.
        ol = registry.build_registry().get("ollama")
        try:
            local_models = ol.list_models() if ol is not None else []
        except Exception:
            local_models = []
        info = catalog.lookup(m)
        if info is not None and m not in local_models:
            imgs = "yes" if info.supports_images else "no"
            console.print(f"{m}: {info.context_window} tokens context · "
                          f"{info.default_max_tokens} max output · images: {imgs} (catalog)")
        else:
            win = ol.context_window(m) if ol is not None else 0
            console.print(f"{m}: {win} tokens (num_ctx 2B will pin on this machine)")
        raise SystemExit(0)

    # License acknowledgment gate — validated on every run before the agent (incl. chat).
    # Metadata/maintenance commands above already exited, so this fires only when using the
    # agent. --yes counts as acceptance (also how install.sh accepts non-interactively). An
    # explicit 'n' uninstalls 2B; Enter/anything else just exits (asked again next run).
    from . import license as _license

    def _decline_uninstall() -> None:
        from . import uninstall
        raise SystemExit(uninstall.run(console.print, lambda _p: True, assume_yes=True))

    if not _license.ensure_accepted(assume_yes=args.yes, interactive=sys.stdin.isatty(),
                                    out=console.print, on_decline=_decline_uninstall):
        raise SystemExit(1)

    try:
        if args.model:
            model = args.model
        else:
            # Prefer a persisted /default, but only if it still resolves (provider
            # reachable / key present); otherwise fall back to local autodetect.
            from . import registry
            saved = config.get_prefs().get("default_model")
            model = saved if (saved and registry.resolve(registry.build_registry(), saved) is not None) else None
            model = model or orchestrator.pick_default_model()
    except SystemExit:
        # No model available. Offer first-run onboarding instead of just erroring out.
        from . import setup
        if not args.model and sys.stdin.isatty() and setup._confirm(
                "No local model is set up yet. Run first-time setup now?", True, {}):
            setup.run({})
            try:
                model = orchestrator.pick_default_model()
            except SystemExit:
                raise
        else:
            raise
    if not args.model:
        src = "saved default" if config.get_prefs().get("default_model") == model else "autodetected"
        console.print(f"[dim]No --model given, {src}: {model}[/dim]")

    # Best-effort update notice (from a prior background check; never blocks startup).
    from . import update
    _note = update.notice()
    if _note:
        console.print(f"[dim]{_note}[/dim]")

    # Connect any MCP servers with enabled tools (no-op if none are configured).
    from . import mcp_client
    mcp_client.manager.start()

    # Resume a saved conversation (--continue = most recent here; --resume ID = specific).
    # The loaded thread is attached to the first task created, so the next message
    # continues it.
    resume_conv = None
    resume_id = None
    if args.cont or args.resume:
        from . import persist
        sid = args.resume or persist.most_recent_id(os.getcwd())
        resume_conv = persist.load(sid, cwd=os.getcwd()) if sid else None
        if resume_conv is None:
            console.print("[yellow]Nothing to resume in this directory.[/yellow]" if args.cont
                          else f"[yellow]No saved session '{args.resume}' in this directory.[/yellow]")
        else:
            resume_id = sid   # first task adopts this id so its save updates the same row
            console.print(f"[dim]Resuming {sid} — {len(resume_conv.messages)} messages. "
                          "Your next message continues it.[/dim]")

    # Full-screen Textual TUI by default on an interactive terminal; the proven
    # line-mode REPL for --classic and for scripted/piped (non-TTY) use.
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and not args.classic:
        from .app_tui import run_tui
        run_tui(model, args.yes, args.task, args.theme, resume_conv=resume_conv, resume_id=resume_id)
    else:
        App(model=model, auto_yes=args.yes, resume_conv=resume_conv,
            resume_id=resume_id).run(initial_task=args.task)


if __name__ == "__main__":
    main()
