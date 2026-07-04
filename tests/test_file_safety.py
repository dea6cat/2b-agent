"""Tests for the P20 file-tool safety stack.

Read dedup, the read-loop circuit breaker, streak resetting, and the
read-before-overwrite gate — all host-side, driven through the real dispatcher
with real temp files. The frozen 5-tool schema is untouched.
Run: `python -m unittest tests.test_file_safety`.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(self.proj)
        self.addCleanup(os.chdir, cwd)
        self.addCleanup(shutil.rmtree, self.proj, ignore_errors=True)

    def _write(self, rel, content):
        p = os.path.join(self.proj, rel)
        with open(p, "w") as f:
            f.write(content)
        return p

    def _read(self, session, task, path):
        return orchestrator._dispatch_tool(session, task, "read_file", {"path": path})


class ReadDedup(_Base):
    def test_second_identical_read_is_deduped(self):
        self._write("a.py", "x = 1\n")
        s, t = Session(default_model="m"), Task(description="t")
        self.assertIn("x = 1", self._read(s, t, "a.py"))
        second = self._read(s, t, "a.py")
        self.assertIn("unchanged since you read it", second)
        self.assertNotIn("x = 1", second)   # the full body is not re-sent

    def test_read_loop_breaker_after_limit(self):
        self._write("a.py", "x = 1\n")
        s, t = Session(default_model="m"), Task(description="t")
        outs = [self._read(s, t, "a.py") for _ in range(orchestrator.READ_LOOP_LIMIT)]
        self.assertTrue(outs[-1].startswith("error:"))
        self.assertIn("keep re-reading", outs[-1])
        self.assertIn("stop re-reading", outs[-1].lower())

    def test_read_loop_breaker_message_is_stable(self):
        # The breaker text must be identical across repeats (no interpolated count), so
        # the generic _LoopGuard sees an identical result and can hard-stop the task.
        self._write("a.py", "x = 1\n")
        s, t = Session(default_model="m"), Task(description="t")
        outs = [self._read(s, t, "a.py") for _ in range(6)]
        breakers = [o for o in outs if o.startswith("error:")]
        self.assertGreaterEqual(len(breakers), 2)
        self.assertEqual(len(set(breakers)), 1)   # all identical

    def test_intervening_call_resets_streak(self):
        self._write("a.py", "x = 1\n")
        s, t = Session(default_model="m"), Task(description="t")
        self._read(s, t, "a.py")
        orchestrator._dispatch_tool(s, t, "list_files", {"path": "."})   # breaks the streak
        out = self._read(s, t, "a.py")
        self.assertIn("x = 1", out)
        self.assertNotIn("unchanged since you read it", out)

    def test_different_range_of_same_file_is_a_fresh_read(self):
        self._write("a.py", "l1\nl2\nl3\nl4\n")
        s, t = Session(default_model="m"), Task(description="t")
        self._read(s, t, "a.py:1-2")
        out = self._read(s, t, "a.py:3-4")
        self.assertIn("l3", out)
        self.assertNotIn("unchanged since you read it", out)

    def test_change_on_disk_forces_a_real_reread(self):
        p = self._write("a.py", "x = 1\n")
        s, t = Session(default_model="m"), Task(description="t")
        self._read(s, t, "a.py")
        future = time.time() + 5
        with open(p, "w") as f:
            f.write("x = 2\n")
        os.utime(p, (future, future))
        out = self._read(s, t, "a.py")
        self.assertIn("x = 2", out)
        self.assertNotIn("unchanged since you read it", out)


class OverwriteGate(_Base):
    def _session_task(self):
        # accept-edits so a permitted write applies without a confirmation prompt.
        return Session(auto_yes=True, default_model="m"), Task(description="t")

    def test_overwrite_unread_existing_file_is_refused(self):
        p = self._write("cfg.txt", "old\n")
        s, t = self._session_task()
        out = orchestrator.apply_write(s, t, "cfg.txt", "new\n")
        self.assertIn("haven't read it this session", out)
        with open(p) as f:
            self.assertEqual(f.read(), "old\n")   # left untouched

    def test_new_file_write_is_allowed(self):
        s, t = self._session_task()
        out = orchestrator.apply_write(s, t, "new.txt", "hello\n")
        self.assertTrue(out.startswith("wrote"), out)

    def test_overwrite_after_reading_is_allowed(self):
        self._write("cfg.txt", "old\n")
        s, t = self._session_task()
        self._read(s, t, "cfg.txt")
        out = orchestrator.apply_write(s, t, "cfg.txt", "new\n")
        self.assertTrue(out.startswith("wrote"), out)

    def test_overwrite_after_own_write_is_allowed(self):
        # 2B's own write refreshes read-state, so overwriting a file it just wrote is fine.
        s, t = self._session_task()
        orchestrator.apply_write(s, t, "gen.txt", "v1\n")
        out = orchestrator.apply_write(s, t, "gen.txt", "v2\n")
        self.assertTrue(out.startswith("wrote"), out)


if __name__ == "__main__":
    unittest.main()
