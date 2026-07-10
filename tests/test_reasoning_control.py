"""Reasoning resolution precedence (session > TWOB_THINK > None) + Session.think field.
Run: `python -m unittest tests.test_reasoning_control`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator as O  # noqa: E402
from two_b.session import Session  # noqa: E402


class Resolve(unittest.TestCase):
    def setUp(self):
        os.environ.pop("TWOB_THINK", None)
        self.addCleanup(lambda: os.environ.pop("TWOB_THINK", None))

    def test_default_is_none(self):
        self.assertIsNone(O._reasoning_effective(Session()))

    def test_session_override_wins(self):
        s = Session()
        s.think = "off"
        os.environ["TWOB_THINK"] = "high"
        self.assertEqual(O._reasoning_effective(s), "off")

    def test_env_used_when_no_session(self):
        os.environ["TWOB_THINK"] = "low"
        self.assertEqual(O._reasoning_effective(Session()), "low")

    def test_invalid_env_ignored(self):
        os.environ["TWOB_THINK"] = "banana"
        self.assertIsNone(O._reasoning_effective(Session()))

    def test_session_field_defaults_none(self):
        self.assertIsNone(Session().think)


if __name__ == "__main__":
    unittest.main()
