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

from . import __version__, banner, orchestrator
from .commands import dispatch_input
from .prompt import make_session, prompt_line
from .rawkey import CTRL_B, KeyListener
from .orchestrator import AgentEvent, EventType
from .session import Session, Task, TaskState
from .tui import render_session


class App:
    def __init__(self, model: str, auto_yes: bool):
        # Pin the Console to the real stdout at startup so a worker thread's
        # redirect_stdout (used to capture verbatim-tool print output) can never
        # divert the UI's terminal writes.
        self.console = Console(file=sys.stdout)
        self.session = Session(default_model=model, auto_yes=auto_yes, cwd=os.getcwd())
        self.listener = KeyListener(on_key=self._on_key)
        self._session = make_session()
        self._quit = False
        self._background_requested = False
        self._fg_target: str | None = None

    # --- callbacks used by commands.py (duck-typed App interface) ---
    def request_quit(self) -> None:
        self._quit = True

    def request_foreground(self, task_id: str) -> None:
        self._fg_target = task_id

    def enqueue_task(self, description: str) -> Task:
        return self.session.add_task(description)

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

    def _drain_events(self) -> None:
        q = self.session.events
        while not q.empty():
            self._print_event(q.get())

    # --- the watch loop for the foregrounded task ---
    def _watch(self, task: Task) -> None:
        self.session.active_task_id = task.id
        if task.state == TaskState.QUEUED:
            task.state = TaskState.ACTIVE
            task.thread = threading.Thread(
                target=orchestrator.run_task,
                args=(self.session, task, self.session.events.put),
                daemon=True,
            )
            task.thread.start()
        elif task.state == TaskState.BACKGROUNDED:
            task.state = TaskState.ACTIVE
        self._watch_loop(task)

    def _watch_loop(self, task: Task) -> None:
        from rich.live import Live

        # The key listener (raw/cbreak mode) is active ONLY while watching a
        # running task, so it never competes with the idle REPL prompt's
        # input(). Confirmation prompts inside this loop use listener.paused().
        self._background_requested = False
        self.listener.start()
        try:
            while True:
                action = "done"
                with Live(render_session(self.session), console=self.console,
                          refresh_per_second=15, transient=True) as live:
                    while True:
                        self._drain_events_live(live)
                        if task.pending is not None:
                            action = "confirm"
                            break
                        if self._background_requested:
                            action = "background"
                            break
                        if (task.thread is None or not task.thread.is_alive()) and self.session.events.empty():
                            action = "done"
                            break
                        live.update(render_session(self.session))
                        time.sleep(0.06)
                # Live has fully exited here — safe to prompt / print.
                if action == "confirm":
                    self._prompt_confirmation(task)
                    continue
                if action == "background":
                    self._background_requested = False
                    task.state = TaskState.BACKGROUNDED
                    self.console.print(
                        f"[dim]Backgrounded [{task.id}] {task.title}. /tasks to check, /fg {task.id} to resume.[/dim]"
                    )
                    self.session.active_task_id = None
                    return
                # done
                self._drain_events()
                self.session.active_task_id = None
                return
        finally:
            self.listener.stop()

    def _drain_events_live(self, live) -> None:
        q = self.session.events
        while not q.empty():
            self._print_event(q.get())

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
    parser = argparse.ArgumentParser(
        prog="2b",
        description="A local-first coding agent that keeps small models focused.",
    )
    parser.add_argument("--model", help="Ollama model tag, e.g. qwen3.5:9b (default: autodetect)")
    parser.add_argument("--yes", action="store_true", help="Auto-apply file writes/edits without confirmation")
    parser.add_argument("--list-models", action="store_true", help="List installed Ollama models and exit")
    parser.add_argument("--version", action="version", version=f"2b {__version__}")
    parser.add_argument("task", nargs="?", help="An initial task to run before dropping into the session")
    args = parser.parse_args()

    console = Console()

    if args.list_models:
        try:
            models = orchestrator.list_installed_models()
        except Exception as e:
            console.print(f"[red]Could not reach Ollama at {orchestrator.ollama_host()}: {e}[/red]")
            raise SystemExit(1)
        if not models:
            console.print(f"No models installed in Ollama at {orchestrator.ollama_host()}.")
        else:
            for m in models:
                marker = "  (default)" if m == orchestrator.DEFAULT_MODEL else ""
                console.print(f"{m}{marker}")
        raise SystemExit(0)

    try:
        model = args.model or orchestrator.pick_default_model()
    except SystemExit:
        raise
    if not args.model:
        console.print(f"[dim]No --model given, autodetected: {model}[/dim]")

    App(model=model, auto_yes=args.yes).run(initial_task=args.task)


if __name__ == "__main__":
    main()
