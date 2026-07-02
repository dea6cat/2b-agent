"""Interactive input layer (prompt_toolkit): the idle REPL prompt with a live,
filter-as-you-type `/` command menu.

This owns the terminal ONLY while the user is typing at the idle prompt. It does
not run during task execution (rich.Live + the termios ctrl+b listener handle
that), so the two never contend for the terminal.
"""
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI

from . import registry
from .commands import command_specs


class SlashCompleter(Completer):
    """Completes slash commands and their arguments:
      - the command token itself (with descriptions in the meta column),
      - model names after `/model ` (from configured providers, cached),
      - file paths after `/add `.
    Yields nothing for normal task text, so typing a task doesn't pop a menu.
    """

    def __init__(self, app=None):
        self._specs = command_specs()
        self._app = app
        self._paths = PathCompleter(expanduser=True)

    def _models(self):
        out = []
        if self._app is None:
            return out
        for pname, prov in registry.usable(self._app.registry).items():
            try:
                for m in prov.list_models():
                    out.append(f"{pname}:{m}")
            except Exception:
                continue
        return out

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:                       # completing the command name
            word = text[1:]
            for name, doc in self._specs:
                if name.startswith(word):
                    yield Completion(name, start_position=-len(word),
                                     display=f"/{name}", display_meta=doc)
            return
        cmd, _, arg = text[1:].partition(" ")      # completing an argument
        if cmd == "model":
            for full in self._models():
                if arg.lower() in full.lower():
                    yield Completion(full, start_position=-len(arg))
        elif cmd == "add":
            sub = Document(arg, cursor_position=len(arg))
            yield from self._paths.get_completions(sub, complete_event)


def make_session(app=None) -> PromptSession | None:
    """The interactive completion prompt only makes sense on a real terminal.
    On a non-TTY (piped/scripted input) return None so prompt_line falls back to
    plain input() instead of prompt_toolkit's noisy non-TTY path."""
    if not sys.stdin.isatty():
        return None
    return PromptSession(
        completer=SlashCompleter(app),
        complete_while_typing=True,
    )


def prompt_line(session: "PromptSession | None", model: str) -> str:
    """Read one line at the idle prompt. Raises EOFError on ctrl+D and
    KeyboardInterrupt on ctrl+C, same as input(), so callers handle them."""
    if session is None:
        return input(f"\n[2b · {model}] > ")
    message = ANSI(f"\n\x1b[1m[2b · {model}]\x1b[0m › ")
    return session.prompt(message)
