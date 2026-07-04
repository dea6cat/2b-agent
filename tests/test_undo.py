"""Tests for multi-level /undo (Phase 3.3). Drives the real commands._undo with a
fake app over real temp files. Run: `python -m unittest tests.test_undo`.
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import commands  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


def _app_with(task):
    s = Session()
    s.tasks = [task]
    return types.SimpleNamespace(session=s, ui=types.SimpleNamespace(print=lambda *a, **k: None))


class MultiLevelUndo(unittest.TestCase):
    def setUp(self):
        # /undo now mirrors to the durable undo log; point it at a temp dir so tests
        # don't touch the real ~/.config/2b/undo.
        self._undo_dir = tempfile.mkdtemp()
        os.environ["TWOB_UNDO_DIR"] = self._undo_dir
        self.addCleanup(lambda: __import__("shutil").rmtree(self._undo_dir, ignore_errors=True))
        self.addCleanup(os.environ.pop, "TWOB_UNDO_DIR", None)

    def _tmp(self, content):
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write(content)
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        return f.name

    def test_undo_walks_back_multiple_edits(self):
        p = self._tmp("C")                         # file currently at C
        t = Task(description="x")
        t.push_edit(p, "A")                         # edit A->B recorded pre=A
        t.push_edit(p, "B")                         # edit B->C recorded pre=B
        app = _app_with(t)
        commands._undo("", app)                     # -> back to B
        self.assertEqual(open(p).read(), "B")
        commands._undo("", app)                     # -> back to A
        self.assertEqual(open(p).read(), "A")
        self.assertEqual(t.edit_history, [])
        commands._undo("", app)                     # nothing left (no raise)
        self.assertEqual(open(p).read(), "A")

    def test_undo_n_reverts_last_n(self):
        p = self._tmp("C")
        t = Task(description="x")
        t.push_edit(p, "A")
        t.push_edit(p, "B")
        commands._undo("2", _app_with(t))           # revert both in one go -> A
        self.assertEqual(open(p).read(), "A")
        self.assertEqual(t.edit_history, [])

    def test_undo_specific_path(self):
        p1, p2 = self._tmp("1b"), self._tmp("2b")
        t = Task(description="x")
        t.push_edit(p1, "1a")
        t.push_edit(p2, "2a")                        # most recent overall is p2
        commands._undo(p1, _app_with(t))             # target p1 explicitly
        self.assertEqual(open(p1).read(), "1a")      # p1 reverted
        self.assertEqual(open(p2).read(), "2b")      # p2 untouched
        self.assertEqual(t.edit_history, [(p2, "2a")])  # only p1's entry popped

    def test_undo_removes_newly_created_file(self):
        p = self._tmp("new content")
        t = Task(description="x")
        t.push_edit(p, None)                          # None pre = file was newly created
        commands._undo("", _app_with(t))
        self.assertFalse(os.path.exists(p))           # undo removes it

    def test_push_edit_caps_history(self):
        t = Task(description="x")
        for i in range(60):
            t.push_edit(f"f{i}", str(i))
        self.assertEqual(len(t.edit_history), 50)     # capped
        self.assertEqual(t.edit_history[-1], ("f59", "59"))  # newest kept


if __name__ == "__main__":
    unittest.main()
