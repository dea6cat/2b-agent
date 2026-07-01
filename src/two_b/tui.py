"""Milestone-1 TUI: a live status line with spinner + elapsed timer.

Design constraint carried from the whole project: the TUI only *observes* the
orchestrator's events. It never touches the model I/O path or the tool schema.

Interaction model for M1: the rich.Live region runs while the model is
generating (TURN_START). As soon as tool calls come back, Live is stopped so
the ported do_* functions' own print()/input() (diff previews, confirmation
prompts) work on a clean terminal; tool activity is logged to scrollback.
Live restarts on the next TURN_START. This keeps tools.py verbatim.
"""
import time

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from .orchestrator import AgentEvent, EventType

# Compact one-word labels for the status line while the model works after a tool.
_AFTER_TOOL_STATUS = {
    "list_files": "Listing files",
    "read_file": "Reading",
    "search_files": "Searching",
    "edit_file": "Editing",
    "write_file": "Writing",
}


class StatusTUI:
    """Renders a single task's live progress for Milestone 1."""

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self._live: Live | None = None
        self._turn_started_at = 0.0
        self._status = "Working"

    def _render(self):
        elapsed = int(time.monotonic() - self._turn_started_at)
        return Group(
            Spinner("dots", text=Text(f" {self._status}… ({elapsed}s)")),
            Text("  (ctrl+b to run in background)", style="dim"),
        )

    def _start_live(self):
        if self._live is None:
            self._turn_started_at = time.monotonic()
            self._live = Live(
                self._render(),
                console=self.console,
                refresh_per_second=4,
                transient=True,  # clear the spinner when generation ends
            )
            self._live.start()

    def _stop_live(self):
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _refresh(self):
        if self._live is not None:
            self._live.update(self._render())

    def handle(self, event: AgentEvent) -> None:
        if event.type == EventType.TURN_START:
            self._start_live()
            self._refresh()

        elif event.type == EventType.TOOL_CALL_START:
            self._stop_live()
            name = event.payload["name"]
            args = event.payload["arguments"]
            # Hide large content payloads in the log line, matching the prototype.
            shown = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in args.items()}
            self.console.print(Text(f"→ {name} {shown}", style="cyan"))
            # Prime the status shown on the NEXT generation turn.
            self._status = _AFTER_TOOL_STATUS.get(name, "Working")

        elif event.type == EventType.TOOL_CALL_RESULT:
            result = event.payload["result"]
            first_line = result.splitlines()[0] if result else ""
            style = "red" if first_line.startswith("error") else "dim"
            self.console.print(Text(f"  {first_line[:200]}", style=style))

        elif event.type == EventType.ASSISTANT_TEXT:
            self._stop_live()
            self.console.print()
            self.console.print(Text(event.payload["text"]))

        elif event.type == EventType.TASK_DONE:
            self._stop_live()

        elif event.type == EventType.TASK_ERROR:
            self._stop_live()
            self.console.print(Text(f"error: {event.payload.get('error', 'unknown')}", style="bold red"))

    def close(self) -> None:
        self._stop_live()
