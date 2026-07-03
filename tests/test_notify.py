"""Tests for the finish-notification escape builder (Phase 4.5). The OSC-9 building
and the enable gate are pure/testable; the /dev/tty write isn't unit-tested. Run:
`python -m unittest tests.test_notify`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import notify  # noqa: E402


class Osc9(unittest.TestCase):
    def test_wraps_body_in_osc9(self):
        self.assertEqual(notify.osc9("done: fix bug"), "\x1b]9;done: fix bug\x07")

    def test_strips_control_chars_that_would_break_the_sequence(self):
        out = notify.osc9("a\x1bb\x07c\nd")
        self.assertNotIn("\x1b]9;a\x1b", out)          # embedded ESC neutralized
        self.assertTrue(out.startswith("\x1b]9;"))
        self.assertTrue(out.endswith("\x07"))
        self.assertNotIn("\n", out)
        # exactly one terminator (the trailing BEL), none injected mid-body
        self.assertEqual(out.count("\x07"), 1)

    def test_empty_body_still_builds(self):
        self.assertEqual(notify.osc9(""), "\x1b]9;\x07")
        self.assertEqual(notify.osc9(None), "\x1b]9;\x07")


class Gate(unittest.TestCase):
    def test_enabled_by_default(self):
        os.environ.pop("TWOB_NO_NOTIFY", None)
        self.assertTrue(notify.enabled())

    def test_disabled_by_env(self):
        os.environ["TWOB_NO_NOTIFY"] = "1"
        self.addCleanup(lambda: os.environ.pop("TWOB_NO_NOTIFY", None))
        self.assertFalse(notify.enabled())
        self.assertFalse(notify.send("anything"))     # gate short-circuits before any tty write

    def test_blank_body_is_noop(self):
        os.environ.pop("TWOB_NO_NOTIFY", None)
        self.assertFalse(notify.send("   "))


if __name__ == "__main__":
    unittest.main()
