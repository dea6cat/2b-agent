"""Tests for the durable undo log (changelog.py) — persistence + resume behavior.

The undo stack survives a restart: edits recorded in one "session" can be reverted
in a fresh Task that adopts the same id. Host-side, stdlib. Run:
`python -m unittest tests.test_changelog_undo`.
"""
import os
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import changelog, commands  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class Persistence(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["TWOB_UNDO_DIR"] = self.d
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.addCleanup(os.environ.pop, "TWOB_UNDO_DIR", None)

    def test_save_load_roundtrip(self):
        hist = [("a.py", "old a"), ("b.py", None)]     # modify + create
        changelog.save("task1", "/proj", hist)
        self.assertEqual(changelog.load("task1", "/proj"), [("a.py", "old a"), ("b.py", None)])

    def test_unknown_task_loads_empty(self):
        self.assertEqual(changelog.load("nope", "/proj"), [])

    def test_scoped_by_task_and_cwd(self):
        changelog.save("t", "/proj-a", [("x", "1")])
        changelog.save("t", "/proj-b", [("y", "2")])
        self.assertEqual(changelog.load("t", "/proj-a"), [("x", "1")])
        self.assertEqual(changelog.load("t", "/proj-b"), [("y", "2")])

    def test_oversize_snapshot_is_dropped_on_load(self):
        big = "x" * (changelog.MAX_SNAPSHOT_BYTES + 1)
        changelog.save("t", "/proj", [("small.py", "ok"), ("huge.py", big)])
        loaded = changelog.load("t", "/proj")
        self.assertEqual(loaded, [("small.py", "ok")])   # huge one skipped, not restored wrong

    def test_atomic_write_leaves_no_tmp(self):
        changelog.save("t", "/proj", [("a", "1")])
        self.assertFalse(any(n.endswith(".tmp") for n in os.listdir(self.d)))

    def test_disabled_is_noop(self):
        os.environ["TWOB_NO_HISTORY"] = "1"
        try:
            changelog.save("t", "/proj", [("a", "1")])
            self.assertEqual(changelog.load("t", "/proj"), [])
        finally:
            del os.environ["TWOB_NO_HISTORY"]


class DurableUndoAcrossResume(unittest.TestCase):
    """Simulates: edit in session 1 (persist), quit, resume into a fresh Task, /undo."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["TWOB_UNDO_DIR"] = self.d
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.addCleanup(os.environ.pop, "TWOB_UNDO_DIR", None)

    def _app(self, task):
        s = Session()
        s.tasks = [task]
        return types.SimpleNamespace(session=s, ui=types.SimpleNamespace(print=lambda *a, **k: None))

    def test_undo_reverts_an_edit_from_a_prior_session(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write("EDITED")                                # file as left by session 1
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))

        # Session 1 recorded that the pre-edit content was "ORIGINAL".
        changelog.save("sess1", os.getcwd(), [(f.name, "ORIGINAL")])

        # Session 2: a fresh Task adopts the id and restores its undo stack from disk.
        task = Task(description="resumed")
        task.id = "sess1"
        task.edit_history = changelog.load("sess1", os.getcwd())
        self.assertEqual(task.edit_history, [(f.name, "ORIGINAL")])

        commands._undo("", self._app(task))
        self.assertEqual(open(f.name).read(), "ORIGINAL")   # reverted across the "restart"
        self.assertEqual(task.edit_history, [])
        # And the durable log was re-synced so a second resume won't re-offer it.
        self.assertEqual(changelog.load("sess1", os.getcwd()), [])


if __name__ == "__main__":
    unittest.main()
