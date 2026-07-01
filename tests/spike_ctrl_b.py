"""Interactive spike — run in a REAL terminal (not piped):

    cd /Users/do519-lap/repo_apps/2B && python3 tests/spike_ctrl_b.py

It runs a live spinner+timer (like 2B's TUI), listens for ctrl+b in the
background, and on ctrl+b stops the live region, restores canonical mode, and
runs a normal input() prompt — proving the exact pause-on-write handoff M2
relies on. Press 'q' to quit, ctrl+b to trigger the prompt.

Expected: the spinner ticks; pressing ctrl+b cleanly shows the input prompt and
your typed answer echoes normally; afterward the spinner resumes; 'q' exits and
the terminal is left in a normal state (no stuck raw mode).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from two_b.rawkey import CTRL_B, KeyListener, stdin_is_interactive

console = Console()

if not stdin_is_interactive():
    console.print("[red]stdin is not a TTY — run this directly in a terminal, not piped.[/red]")
    sys.exit(1)

state = {"background_requested": False, "quit": False}


def on_key(ch: str) -> None:
    if ch == CTRL_B:
        state["background_requested"] = True
    elif ch == "q":
        state["quit"] = True


listener = KeyListener(on_key=on_key)
listener.start()
console.print("Spinner running. Press [bold]ctrl+b[/bold] to simulate a background write prompt, [bold]q[/bold] to quit.")

started = time.monotonic()
try:
    while not state["quit"]:
        with Live(console=console, refresh_per_second=8, transient=True) as live:
            while not state["background_requested"] and not state["quit"]:
                elapsed = int(time.monotonic() - started)
                live.update(Spinner("dots", text=Text(f" Working… ({elapsed}s)")))
                time.sleep(0.1)
        if state["quit"]:
            break
        if state["background_requested"]:
            state["background_requested"] = False
            with listener.paused():
                answer = input("\n[paused] Apply this write? [y/N] ")
            console.print(f"[green]You answered:[/green] {answer!r} — resuming spinner.")
finally:
    listener.stop()
    console.print("Exited cleanly. Terminal should be back to normal.")
