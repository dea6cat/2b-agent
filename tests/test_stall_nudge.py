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

    def test_signoff_is_not_a_stall(self):
        self.assertFalse(O._stalled_without_acting("Let me know if you have any other questions."))
        self.assertFalse(O._stalled_without_acting("I can now confirm the fix works as expected."))

    def test_intent_needs_an_investigative_verb(self):
        self.assertTrue(O._stalled_without_acting("Let me first explore the project structure."))
        self.assertTrue(O._stalled_without_acting("I'll look at the files to understand this."))
        self.assertTrue(O._stalled_without_acting("I need to read the README first."))


if __name__ == "__main__":
    unittest.main()
