import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from two_b import orchestrator
from two_b.session import Session, Task, MODE_ACCEPT, MODE_NORMAL, MODE_PLAN


def _app(mode):
    s = Session.__new__(Session)
    s.mode = mode
    t = Task.__new__(Task)
    t.last_edit_snapshot = None
    t.last_diff = None
    return s, t


class ApplyWorkerChanges(unittest.TestCase):
    def test_applies_non_conflicting(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        f.write("v = 1\n")
        f.close()
        try:
            s, t = _app(MODE_ACCEPT)
            out = orchestrator.apply_worker_changes(s, t, [(f.name, "v = 1\n", "v = 2\n", 0)])
            with open(f.name) as fh:
                self.assertEqual(fh.read(), "v = 2\n")
            self.assertIn("applied", out.lower())
            self.assertEqual(t.last_edit_snapshot, (f.name, "v = 1\n"))
        finally:
            os.unlink(f.name)

    def test_conflict_not_applied(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        f.write("v = 1\n")
        f.close()
        try:
            s, t = _app(MODE_ACCEPT)
            out = orchestrator.apply_worker_changes(
                s, t, [(f.name, "v = 1\n", "v = 2\n", 0), (f.name, "v = 1\n", "v = 3\n", 1)]
            )
            with open(f.name) as fh:
                self.assertEqual(fh.read(), "v = 1\n")  # untouched
            self.assertIn("conflict", out.lower())
        finally:
            os.unlink(f.name)

    def test_plan_mode_applies_nothing(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        f.write("v = 1\n")
        f.close()
        try:
            s, t = _app(MODE_PLAN)
            out = orchestrator.apply_worker_changes(s, t, [(f.name, "v = 1\n", "v = 2\n", 0)])
            with open(f.name) as fh:
                self.assertEqual(fh.read(), "v = 1\n")
            self.assertIn("plan mode", out.lower())
        finally:
            os.unlink(f.name)

    def test_empty_changes_returns_empty_string(self):
        s, t = _app(MODE_ACCEPT)
        self.assertEqual(orchestrator.apply_worker_changes(s, t, []), "")

    def test_rejected_confirmation_not_applied(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        f.write("v = 1\n")
        f.close()
        original_request_confirmation = orchestrator.request_confirmation
        orchestrator.request_confirmation = lambda *args, **kwargs: False
        try:
            s, t = _app(MODE_NORMAL)
            out = orchestrator.apply_worker_changes(s, t, [(f.name, "v = 1\n", "v = 2\n", 0)])
            with open(f.name) as fh:
                self.assertEqual(fh.read(), "v = 1\n")  # untouched
            self.assertIn("rejected", out.lower())
        finally:
            orchestrator.request_confirmation = original_request_confirmation
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
