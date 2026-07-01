"""Non-blocking single-key capture for ctrl+b (background) while a task runs.

The risk this module de-risks: a raw-mode stdin reader must coexist with a
running rich.Live region (which writes escape sequences to stdout) AND with the
ported tools' plain input() confirmation prompts (which need canonical line
mode). The design keeps those concerns separate:

  - stdin line-discipline (raw/cbreak vs canonical) is toggled here, on demand.
  - rich.Live only ever writes stdout — it does not touch stdin — so the two do
    not fight as long as we are not in raw mode at the moment we call input().

Usage:
    listener = KeyListener(on_key)   # on_key(ch: str) called from a daemon thread
    listener.start()
    ...
    with listener.paused():          # restores canonical mode for input()
        answer = input("Apply? [y/N] ")
    ...
    listener.stop()

On a non-TTY stdin (pipes, CI, redirected input) this degrades to a no-op:
start()/stop() do nothing and no key events fire. That is the correct behavior
for one-shot/scripted use where there is no interactive backgrounding.
"""
import contextlib
import os
import sys
import threading

try:  # POSIX only; matches the macOS/Linux scope of the project.
    import termios
    import tty
    _HAVE_TERMIOS = True
except ImportError:  # pragma: no cover - Windows
    _HAVE_TERMIOS = False


def stdin_is_interactive() -> bool:
    return _HAVE_TERMIOS and sys.stdin is not None and sys.stdin.isatty()


class KeyListener:
    """Reads single keystrokes from a TTY on a daemon thread and dispatches
    them to a callback. No-op when stdin is not an interactive TTY."""

    def __init__(self, on_key):
        self._on_key = on_key
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._fd = sys.stdin.fileno() if stdin_is_interactive() else None
        self._saved_term = None

    @property
    def active(self) -> bool:
        return self._fd is not None

    def start(self) -> None:
        if not self.active or self._thread is not None:
            return
        # Clear flags so a listener that was previously stopped can restart
        # (stop() sets _stop; without this the new thread would exit at once).
        self._stop.clear()
        self._paused.clear()
        self._saved_term = termios.tcgetattr(self._fd)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            tty.setcbreak(self._fd)
            while not self._stop.is_set():
                if self._paused.is_set():
                    self._stop.wait(0.05)
                    continue
                # select with a short timeout so we can observe stop/pause flags
                import select

                r, _, _ = select.select([self._fd], [], [], 0.1)
                if not r:
                    continue
                ch = os.read(self._fd, 1)
                if not ch:
                    continue
                try:
                    self._on_key(ch.decode(errors="ignore"))
                except Exception:
                    pass  # a callback error must never kill the listener thread
        finally:
            self._restore()

    def _restore(self) -> None:
        if self._fd is not None and self._saved_term is not None:
            with contextlib.suppress(Exception):
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_term)

    @contextlib.contextmanager
    def paused(self):
        """Temporarily restore canonical line mode so a normal input() prompt
        works, then resume raw capture. Safe to use when inactive (no-op)."""
        if not self.active:
            yield
            return
        self._paused.set()
        self._restore()
        try:
            yield
        finally:
            # Re-enter cbreak for continued single-key capture.
            with contextlib.suppress(Exception):
                tty.setcbreak(self._fd)
            self._paused.clear()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._restore()


CTRL_B = "\x02"
