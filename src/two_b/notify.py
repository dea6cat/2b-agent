"""Desktop notification when a task finishes while you're looking elsewhere (Phase
4.5). Uses OSC 9 (iTerm2 / kitty / WezTerm / …), written straight to the controlling
terminal so it never disturbs Textual's alt-screen. Best-effort and off by default
when TWOB_NO_NOTIFY is set. The escape-building is pure so it's unit-testable; the
emit is a tiny /dev/tty write.
"""
import os


def osc9(body: str) -> str:
    """The OSC 9 'post notification' escape for `body`. Control chars that would end or
    corrupt the sequence are stripped so the message can't break the terminal."""
    body = (body or "").replace("\x1b", " ").replace("\x07", " ").replace("\n", " ")
    return f"\x1b]9;{body}\x07"


def enabled() -> bool:
    return not os.environ.get("TWOB_NO_NOTIFY")


def send(body: str) -> bool:
    """Post a desktop notification via the controlling terminal. Returns True if it was
    written. Best-effort — any failure (no tty, unsupported terminal) is a silent no-op."""
    if not enabled() or not (body or "").strip():
        return False
    try:
        fd = os.open("/dev/tty", os.O_WRONLY)
        try:
            os.write(fd, osc9(body).encode("utf-8", "replace"))
        finally:
            os.close(fd)
        return True
    except OSError:
        return False
