"""Importing app_tui disables Textual's mouse reporting entirely (2B is keyboard-driven —
scroll is Shift+arrows / PageUp-Down), so mouse movement never floods the app on terminals
(e.g. Terminal.app) that mishandle motion reporting. Guarded on textual (runtime-only dep).
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
class MouseDisabled(unittest.TestCase):
    def test_patch_is_installed(self):
        self.assertTrue(getattr(LinuxDriver._enable_mouse_support, "_2b_no_mouse", False))

    def test_no_mouse_modes_are_enabled(self):
        f = _FakeDriver(mouse=True)
        LinuxDriver._enable_mouse_support(f)
        for mode in ("1000", "1002", "1003", "1015", "1006"):
            self.assertNotIn(f"\x1b[?{mode}h", f.written)   # nothing turned ON

    def test_all_mouse_modes_are_turned_off(self):
        f = _FakeDriver(mouse=True)
        LinuxDriver._enable_mouse_support(f)
        for mode in ("1000", "1002", "1003", "1006"):
            self.assertIn(f"\x1b[?{mode}l", f.written)      # explicitly turned OFF


if __name__ == "__main__":
    unittest.main()
