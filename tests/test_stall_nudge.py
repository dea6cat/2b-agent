import unittest
from two_b import orchestrator as O


class StallDetectTest(unittest.TestCase):
    def test_intent_without_tool_name_is_a_stall(self):
        self.assertTrue(O._stalled_without_acting(
            "I'll help you understand this package. Let me first explore the project structure."))
        self.assertTrue(O._stalled_without_acting("Let me look at the files to understand this."))

    def test_delivered_answer_is_not_a_stall(self):
        # No forward-intent phrasing -> a real answer, must not be flagged.
        self.assertFalse(O._stalled_without_acting(
            "The package is a Dart agent framework. It exports three classes."))

    def test_empty(self):
        self.assertFalse(O._stalled_without_acting(""))


if __name__ == "__main__":
    unittest.main()
