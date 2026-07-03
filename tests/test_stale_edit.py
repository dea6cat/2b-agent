"""Tests for stale-file / read-before-write detection (Phase 2 edit safety).

The helpers touch only `task.read_mtimes`, so a lightweight stand-in stands for a
real Task. `os.utime` bumps mtime deterministically (no sleeps). Run:
`python -m unittest tests.test_stale_edit` from the repo root.
"""
import os
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402


def _task():
    return types.SimpleNamespace(read_mtimes={})


class StaleEdit(unittest.TestCase):
    def _tmp(self, content="x = 1\n"):
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_never_read_file_is_allowed(self):
        # 2B does not force a read-before-write — an unread file passes.
        self.assertEqual(orchestrator._stale_check(_task(), self._tmp()), "")

    def test_read_then_unchanged_is_not_stale(self):
        t, p = _task(), self._tmp()
        orchestrator._record_read(t, p)
        self.assertEqual(orchestrator._stale_check(t, p), "")

    def test_external_change_after_read_is_flagged(self):
        t, p = _task(), self._tmp()
        orchestrator._record_read(t, p)
        future = time.time() + 5           # simulate an out-of-band edit bumping mtime
        os.utime(p, (future, future))
        msg = orchestrator._stale_check(t, p)
        self.assertIn("changed on disk", msg)

    def test_refresh_clears_staleness(self):
        # 2B's own write refreshes the recorded mtime so the next edit isn't flagged.
        t, p = _task(), self._tmp()
        orchestrator._record_read(t, p)
        future = time.time() + 5
        os.utime(p, (future, future))
        self.assertIn("changed on disk", orchestrator._stale_check(t, p))
        orchestrator._refresh_mtime(t, p)                 # as if 2B just wrote it
        self.assertEqual(orchestrator._stale_check(t, p), "")

    def test_record_read_strips_range_suffix(self):
        t, p = _task(), self._tmp("a\nb\nc\n")
        orchestrator._record_read(t, f"{p}:1-2")          # a section read
        self.assertIn(os.path.abspath(p), t.read_mtimes)  # keyed on the bare file

    def test_missing_file_does_not_raise(self):
        t = _task()
        self.assertEqual(orchestrator._stale_check(t, "/no/such/file.py"), "")
        orchestrator._record_read(t, "/no/such/file.py")   # no-op, no raise
        self.assertEqual(t.read_mtimes, {})

    def test_record_read_tracks_basename_fallback_file(self):
        # The Critical case: model reads an imprecise path; do_read_file resolves it to
        # the unique project match, and _record_read must key on THAT real file — else
        # a later edit to the corrected path is never stale-checked.
        import two_b.tools as tools
        proj = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(proj, ignore_errors=True))
        os.makedirs(os.path.join(proj, "sub"))
        real = os.path.join(proj, "sub", "widget.dart")
        with open(real, "w") as f:
            f.write("// widget\n")
        cwd = os.getcwd()
        os.chdir(proj)
        self.addCleanup(os.chdir, cwd)
        # resolve_read_path finds the unique match for the bare basename...
        resolved = tools.resolve_read_path("widget.dart")
        self.assertEqual(os.path.realpath(resolved), os.path.realpath(real))
        # ...and _record_read keys on that resolved file, so a stale check engages.
        t = _task()
        orchestrator._record_read(t, "widget.dart")
        self.assertIn(resolved, t.read_mtimes)
        future = time.time() + 5
        os.utime(real, (future, future))
        self.assertIn("changed on disk", orchestrator._stale_check(t, "sub/widget.dart"))


if __name__ == "__main__":
    unittest.main()
