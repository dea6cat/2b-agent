"""Pure thinking-render helpers.
Run: `python -m unittest tests.test_thinking_render`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import tui  # noqa: E402


class ThinkingRender(unittest.TestCase):
    def test_summary(self):
        self.assertEqual(tui.thinking_summary(12.3), "💭 thought for 12s")
        self.assertEqual(tui.thinking_summary(0.4), "💭 thought for 0s")

    def test_line_is_dim(self):
        t = tui.thinking_line("weighing options")
        self.assertIn("weighing options", t.plain)
        self.assertEqual(t.style, "dim")


if __name__ == "__main__":
    unittest.main()
