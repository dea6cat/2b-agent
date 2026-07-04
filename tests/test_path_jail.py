"""Tests for the path jail: escapes_root + the unattended-write confinement.

An auto-applied write (accept-edits / granted) is confined to the workspace root;
an interactive (individually-confirmed) write is not. Host-side. Run:
`python -m unittest tests.test_path_jail`.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import cmdguard, orchestrator  # noqa: E402
from two_b.session import MODE_NORMAL, Session, Task  # noqa: E402


class EscapesRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_inside_is_not_an_escape(self):
        self.assertFalse(cmdguard.escapes_root(os.path.join(self.root, "a/b.txt"), self.root))

    def test_parent_traversal_escapes(self):
        self.assertTrue(cmdguard.escapes_root(os.path.join(self.root, "../evil.txt"), self.root))
        self.assertTrue(cmdguard.escapes_root("/etc/passwd", self.root))

    def test_symlink_escape_is_caught(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        link = os.path.join(self.root, "link")
        os.symlink(outside, link)                       # a symlink inside root → outside
        self.assertTrue(cmdguard.escapes_root(os.path.join(link, "x.txt"), self.root))


class UnattendedWriteJail(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(self.proj)
        self.addCleanup(os.chdir, cwd)
        self.addCleanup(shutil.rmtree, self.proj, ignore_errors=True)
        # a target OUTSIDE the project
        self.outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)

    def test_accept_edits_write_outside_is_refused(self):
        s, t = Session(auto_yes=True, default_model="m"), Task(description="x")   # accept-edits = unattended
        out = orchestrator.apply_write(s, t, os.path.join(self.outside, "escape.txt"), "x\n")
        self.assertIn("outside the workspace", out)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "escape.txt")))

    def test_accept_edits_write_inside_is_allowed(self):
        s, t = Session(auto_yes=True, default_model="m"), Task(description="x")
        out = orchestrator.apply_write(s, t, "inside.txt", "x\n")
        self.assertTrue(out.startswith("wrote"), out)

    def test_normal_mode_is_not_jailed(self):
        # In normal mode the write isn't unattended (it gets an individual confirm), so the
        # jail doesn't apply — _jail_blocked returns '' and 2B stays point-anywhere.
        s = Session(default_model="m")            # normal mode, no grant
        self.assertEqual(s.mode, MODE_NORMAL)
        self.assertEqual(orchestrator._jail_blocked(s, "write_file",
                                                    os.path.join(self.outside, "x.txt")), "")

    def test_granted_write_outside_is_refused(self):
        # A per-session "allow write_file" grant also makes the write unattended → jailed.
        s, t = Session(default_model="m"), Task(description="x")
        s.granted.add("write_file")
        out = orchestrator.apply_write(s, t, os.path.join(self.outside, "escape.txt"), "x\n")
        self.assertIn("outside the workspace", out)


if __name__ == "__main__":
    unittest.main()
