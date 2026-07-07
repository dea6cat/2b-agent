"""Importing app_tui disables Textual's any-event (motion) mouse tracking, so mouse
movement no longer floods the app on terminals (e.g. Terminal.app) that mishandle 1003.
Button + SGR tracking stay on. Guarded on textual (runtime-only dep).
Run: `python -m unittest tests.test_mouse_motion`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    import two_b.app_tui  # noqa: F401  — triggers _disable_mouse_motion_tracking()
    from textual.drivers.linux_driver import LinuxDriver
    _HAS_TEXTUAL = True
except ModuleNotFoundError:
    _HAS_TEXTUAL = False


class _FakeDriver:
    """Minimal stand-in exposing what _enable_mouse_support touches."""
    def __init__(self, mouse=True):
        self._mouse = mouse
        self.written = []

    def write(self, s):
        self.written.append(s)

    def flush(self):
        pass


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed (runtime-only dependency)")
class MouseMotionDisabled(unittest.TestCase):
    def test_patch_is_installed(self):
        self.assertTrue(getattr(LinuxDriver._enable_mouse_support, "_2b_no_motion", False))

    def test_downgraded_to_button_event_after_enable(self):
        f = _FakeDriver(mouse=True)
        LinuxDriver._enable_mouse_support(f)
        self.assertIn("\x1b[?1002h", f.written)          # button-event tracking (click/drag/wheel)
        self.assertIn("\x1b[?1003l", f.written)          # any-event (free-hover) turned off
        self.assertEqual(f.written[-1], "\x1b[?1003l")   # ...disabled last, after our 1002h
        self.assertLess(f.written.index("\x1b[?1002h"),
                        f.written.index("\x1b[?1003l"))
        self.assertLess(f.written.index("\x1b[?1003h"),  # off comes after Textual's on
                        f.written.index("\x1b[?1003l"))

    def test_no_writes_when_mouse_off(self):
        f = _FakeDriver(mouse=False)
        LinuxDriver._enable_mouse_support(f)
        self.assertEqual(f.written, [])                  # nothing enabled, nothing to disable


if __name__ == "__main__":
    unittest.main()
