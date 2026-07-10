"""The run_task retrieval hook: a fresh conversation gets a pointer message; a continued one does
not. Run: `python -m unittest tests.test_retrieval_wiring`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator as O  # noqa: E402


class Hook(unittest.TestCase):
    def test_helper_returns_message_when_block_nonempty(self, ):
        msg = O._retrieval_message("BLOCK TEXT")
        self.assertIsNotNone(msg)
        self.assertIn("BLOCK TEXT", msg.text)

    def test_helper_returns_none_on_empty_block(self):
        self.assertIsNone(O._retrieval_message(""))


if __name__ == "__main__":
    unittest.main()
