"""orchestrator.abort_all sets the cancel flag on every active AND backgrounded
task, clears their steer, closes live connections, and returns the count.

Run: `python -m unittest tests.test_abort_all` from the repo root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402
from two_b.providers import base  # noqa: E402
from two_b.session import Session, Task, TaskState  # noqa: E402


class AbortAll(unittest.TestCase):
    def _task(self, state):
        t = Task(description="do a thing")
        t.state = state
        return t

    def test_aborts_active_and_backgrounded_only(self):
        s = Session()
        active = self._task(TaskState.ACTIVE)
        bg = self._task(TaskState.BACKGROUNDED)
        done = self._task(TaskState.DONE)
        s.tasks = [active, bg, done]

        closed = {"n": 0}
        real = base.abort_all_connections
        base.abort_all_connections = lambda: closed.__setitem__("n", closed["n"] + 1)
        self.addCleanup(setattr, base, "abort_all_connections", real)

        n = orchestrator.abort_all(s)

        self.assertEqual(n, 2)
        self.assertTrue(active.cancel_flag.is_set())
        self.assertTrue(bg.cancel_flag.is_set())
        self.assertFalse(done.cancel_flag.is_set())
        self.assertEqual(closed["n"], 1, "must close live connections exactly once")


if __name__ == "__main__":
    unittest.main()
