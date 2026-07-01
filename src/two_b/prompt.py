"""Interactive input layer (prompt_toolkit): the idle REPL prompt with a live,
filter-as-you-type `/` command menu.

This owns the terminal ONLY while the user is typing at the idle prompt. It does
not run during task execution (rich.Live + the termios ctrl+b listener handle
that), so the two never contend for the terminal.
"""
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI

from .commands import command_specs


class SlashCompleter(Completer):
    """Completes slash commands (and only the command token) with descriptions
    shown in the dropdown's meta column. Yields nothing for normal task text, so
    typing a task doesn't pop a menu."""

    def __init__(self):
        self._specs = command_specs()

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        word = text[1:]
        if " " in word:  # only complete the command itself, not its arguments
            return
        for name, doc in self._specs:
            if name.startswith(word):
                yield Completion(
                    name,
                    start_position=-len(word),
                    display=f"/{name}",
                    display_meta=doc,
                )


def make_session() -> PromptSession | None:
    """The interactive completion prompt only makes sense on a real terminal.
    On a non-TTY (piped/scripted input) return None so prompt_line falls back to
    plain input() instead of prompt_toolkit's noisy non-TTY path."""
    if not sys.stdin.isatty():
        return None
    return PromptSession(
        completer=SlashCompleter(),
        complete_while_typing=True,
    )


def prompt_line(session: "PromptSession | None", model: str) -> str:
    """Read one line at the idle prompt. Raises EOFError on ctrl+D and
    KeyboardInterrupt on ctrl+C, same as input(), so callers handle them."""
    if session is None:
        return input(f"\n[2b · {model}] > ")
    message = ANSI(f"\n\x1b[1m[2b · {model}]\x1b[0m › ")
    return session.prompt(message)
