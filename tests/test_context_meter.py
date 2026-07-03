"""Tests for the context-window usage helper (Phase 4.1). Pure logic; the TUI
rendering around it isn't unit-tested. Run: `python -m unittest tests.test_context_meter`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.orchestrator import context_usage  # noqa: E402


class ContextUsage(unittest.TestCase):
    def test_basic_percentages(self):
        self.assertEqual(context_usage(0, 8000), (0, False))
        self.assertEqual(context_usage(4000, 8000), (50, False))
        self.assertEqual(context_usage(6000, 8000), (75, False))

    def test_warning_zone_at_80_percent(self):
        self.assertEqual(context_usage(6400, 8000), (80, True))     # exactly 80 warns
        self.assertEqual(context_usage(7200, 8000), (90, True))

    def test_caps_at_100(self):
        # An over-budget conversation (about to be compacted) reads as 100%, not 137%.
        pct, warn = context_usage(11000, 8000)
        self.assertEqual((pct, warn), (100, True))

    def test_unknown_budget_is_silent(self):
        self.assertEqual(context_usage(5000, 0), (0, False))
        self.assertEqual(context_usage(5000, -1), (0, False))


if __name__ == "__main__":
    unittest.main()
