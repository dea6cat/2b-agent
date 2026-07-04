"""Tests for the S7 sensitive-path guard on the file tools: a read of a secrets file
is confirmed even in normal (point-anywhere) mode, and a write/edit to one re-prompts
even under a grant (force=). Host-side. Run:
`python -m unittest tests.test_sensitive_file_guard`.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class SensitiveReadGuard(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        with open(os.path.join(self.ws, "README.md"), "w") as f:
            f.write("hello world\n")
        self._cwd = os.getcwd()
        os.chdir(self.ws)
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_sensitive_read_prompts_and_declines(self):
        calls = []
        with mock.patch.object(orchestrator, "request_confirmation",
                               side_effect=lambda *a, **k: calls.append((a, k)) or False):
            out = orchestrator._dispatch_tool(Session(default_model="m"), Task(description="x"),
                                              "read_file", {"path": "~/.ssh/id_rsa"})
        self.assertEqual(out, "read rejected by user")
        self.assertTrue(calls, "a sensitive read should prompt")

    def test_normal_read_does_not_prompt(self):
        calls = []
        with mock.patch.object(orchestrator, "request_confirmation",
                               side_effect=lambda *a, **k: calls.append((a, k)) or True):
            out = orchestrator._dispatch_tool(Session(default_model="m"), Task(description="x"),
                                              "read_file", {"path": "README.md"})
        self.assertEqual(calls, [], "an ordinary read must not prompt")
        self.assertIn("hello world", out)

    def test_sensitive_read_in_batch_is_refused_not_leaked(self):
        # A parallel read batch can't safely prompt, so a secrets read is refused
        # (forcing a single read) rather than silently returning the key.
        calls = []
        with mock.patch.object(orchestrator, "request_confirmation",
                               side_effect=lambda *a, **k: calls.append(1) or True):
            out = orchestrator._dispatch_tool(Session(default_model="m"), Task(description="x"),
                                              "read_file", {"path": "~/.ssh/id_rsa"}, batch=True)
        self.assertIn("single read_file call", out)
        self.assertEqual(calls, [], "batch must not prompt; it refuses")

    def test_sensitive_search_is_confirmed(self):
        with mock.patch.object(orchestrator, "request_confirmation", side_effect=lambda *a, **k: False):
            out = orchestrator._dispatch_tool(Session(default_model="m"), Task(description="x"),
                                              "search_files", {"query": "BEGIN RSA", "path": "~/.ssh"})
        self.assertEqual(out, "search rejected by user")

    def test_symlink_to_secret_is_caught(self):
        # An innocuously-named symlink pointing at a secret must not slip past the guard.
        target_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target_dir, ignore_errors=True)
        os.mkdir(os.path.join(target_dir, ".ssh"))
        secret = os.path.join(target_dir, ".ssh", "id_rsa")
        with open(secret, "w") as f:
            f.write("KEY\n")
        link = os.path.join(self.ws, "notes.txt")
        os.symlink(secret, link)
        with mock.patch.object(orchestrator, "request_confirmation", side_effect=lambda *a, **k: False):
            out = orchestrator._dispatch_tool(Session(default_model="m"), Task(description="x"),
                                              "read_file", {"path": "notes.txt"})
        self.assertEqual(out, "read rejected by user")


class SensitiveWriteForcesConfirm(unittest.TestCase):
    def test_write_to_secret_path_forces_prompt(self):
        # A path that matches the sensitive set but does not exist (so the read-before-
        # overwrite gate is skipped and we reach the confirmation).
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        secret = os.path.join(tmp, ".aws", "credentials")
        captured = {}

        def fake_conf(session, task, prompt, diff, grant_key=None, force=False):
            captured["force"] = force
            return False

        with mock.patch.object(orchestrator, "request_confirmation", side_effect=fake_conf):
            out = orchestrator.apply_write(Session(default_model="m"), Task(description="x"), secret, "x")
        self.assertTrue(captured.get("force"), "a write to a secrets path must force a prompt")
        self.assertEqual(out, "write rejected by user")


if __name__ == "__main__":
    unittest.main()
