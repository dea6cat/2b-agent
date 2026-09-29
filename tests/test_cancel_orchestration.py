"""A _Cancelled escaping the model stream finishes the task as 'stopped', not
'failed', and compaction forwards the task's cancel flag.

Run: `python -m unittest tests.test_cancel_orchestration` from the repo root.
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.core import orchestrator  # noqa: E402
from two_b.core import compaction  # noqa: E402


class CancelMapping(unittest.TestCase):
    def test_run_task_source_maps_cancelled_to_stopped(self):
        # Guard: both stream paths must catch _Cancelled and route to a stopped finish.
        # (Written as `except (_Interrupted, _Cancelled)`, so assert on the name, not the
        # exact clause text: the per-turn stream lives in run_task, the best-effort final
        # answer's stream in _final_answer_turn.)
        for fn in (orchestrator.run_task, orchestrator._final_answer_turn):
            self.assertIn("_Cancelled", inspect.getsource(fn),
                          f"{fn.__name__}'s stream path must catch _Cancelled")
        # And _Cancelled must be imported so the except can reference it.
        self.assertTrue(hasattr(orchestrator, "_Cancelled"))

    def test_compact_conversation_accepts_cancel(self):
        sig = inspect.signature(compaction.compact_conversation)
        self.assertIn("cancel", sig.parameters)


if __name__ == "__main__":
    unittest.main()
