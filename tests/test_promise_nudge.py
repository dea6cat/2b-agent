"""Tests for the "narrated a tool call but didn't make one" detector
(orchestrator._promised_tool_but_didnt). Pure host-side logic. Run:
`python -m unittest tests.test_promise_nudge` from the repo root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.orchestrator import _promised_tool_but_didnt  # noqa: E402


class PromiseDetector(unittest.TestCase):
    def test_the_real_case_is_flagged(self):
        # The exact phrasing from the live run that did nothing.
        self.assertTrue(_promised_tool_but_didnt(
            "I'll use edit_file to insert this new getter properly into the file structure."))

    def test_various_future_intent_phrasings(self):
        for t in (
            "Let me edit_file to add the method.",
            "I will now call write_file with the new contents.",
            "I'm going to use edit_file to make the change.",
            "I need to run_git to commit this.",
        ):
            self.assertTrue(_promised_tool_but_didnt(t), t)

    def test_past_tense_done_report_is_not_flagged(self):
        for t in (
            "I edited the file and added the getter.",
            "I used edit_file to add the getter; it's done.",
            "The isString getter is now in ToolParameter.",
            "Done — the change has been applied.",
        ):
            self.assertFalse(_promised_tool_but_didnt(t), t)

    def test_no_tool_name_is_not_flagged(self):
        # Intent phrasing without naming a frozen tool — ordinary prose.
        self.assertFalse(_promised_tool_but_didnt("I'll add a short note explaining the change."))

    def test_second_person_advice_is_not_flagged(self):
        # Describing what the USER could do, not first-person intent.
        self.assertFalse(_promised_tool_but_didnt("You'll want to edit_file to change that."))

    def test_empty_is_not_flagged(self):
        self.assertFalse(_promised_tool_but_didnt(""))
        self.assertFalse(_promised_tool_but_didnt(None))


if __name__ == "__main__":
    unittest.main()
