"""The false-success guard: `_edits_all_failed` flags a turn that finalizes after every
edit attempt errored (edit_history stays empty), so the loop can nudge instead of letting
the model declare done on edits that never applied. Run:
`python -m unittest tests.test_false_done_nudge` from the repo root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator as O  # noqa: E402


class EditsAllFailed(unittest.TestCase):
    def test_no_edits_attempted_is_not_flagged(self):
        # A task that never tried to edit (e.g. a pure question) must not be nudged.
        self.assertFalse(O._edits_all_failed(edit_attempts=0, applied_count=0))

    def test_attempts_but_none_applied_is_flagged(self):
        # Two edit_file/write_file calls, both errored -> edit_history empty -> false done.
        self.assertTrue(O._edits_all_failed(edit_attempts=2, applied_count=0))
        self.assertTrue(O._edits_all_failed(edit_attempts=1, applied_count=0))

    def test_at_least_one_applied_is_not_flagged(self):
        # A partial success (some edits landed) is real work, not a false "done".
        self.assertFalse(O._edits_all_failed(edit_attempts=3, applied_count=1))
        self.assertFalse(O._edits_all_failed(edit_attempts=1, applied_count=1))

    def test_nudge_text_is_corrective(self):
        # The nudge must tell the model nothing was changed and not to report done.
        self.assertIn("did NOT apply", O._FALSE_DONE_NUDGE)
        self.assertIn("Do not report this as done", O._FALSE_DONE_NUDGE)


if __name__ == "__main__":
    unittest.main()
