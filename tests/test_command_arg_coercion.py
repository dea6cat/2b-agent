"""Regression: a model may emit a command tool's string argument as a LIST
(run_git {"args": ["status"]}, run_command {"command": [...]}). The dispatch called
.strip() on it and crashed the whole task with AttributeError. It must instead coerce
the list to a shell-equivalent string (losslessly) and run the command.
Run: `python -m unittest tests.test_command_arg_coercion`.
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator, tools  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class CommandArgStr(unittest.TestCase):
    def test_list_is_joined(self):
        self.assertEqual(tools.command_arg_str(["status"]), "status")

    def test_spaced_parts_round_trip_losslessly(self):
        s = tools.command_arg_str(["commit", "-m", "my message"])
        self.assertEqual(shlex.split(s), ["commit", "-m", "my message"])

    def test_string_passes_through(self):
        self.assertEqual(tools.command_arg_str("status --short"), "status --short")

    def test_none_is_empty(self):
        self.assertEqual(tools.command_arg_str(None), "")


class RunGitListArgRegression(unittest.TestCase):
    def test_list_args_run_instead_of_crashing(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        cwd = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, cwd)

        s = Session(default_model="google:gemini-2.5-flash", cwd=d)
        t = Task(description="check changes")
        out = orchestrator._dispatch_tool(s, t, "run_git", {"args": ["status"]})

        self.assertIsInstance(out, str)
        self.assertNotIn("has no attribute", out)          # the old AttributeError
        self.assertTrue(any(w in out.lower() for w in ("branch", "commit", "nothing")))

    def test_run_git_helper_still_crashes_direct_list_is_a_boundary_concern(self):
        # Documents the contract: _run_git assumes a str (its type hint); coercion is the
        # dispatch boundary's job. The dispatch test above is the real regression guard.
        s = Session(default_model="google:gemini-2.5-flash")
        self.assertEqual(orchestrator._run_git(s, Task(description="x"), "", None),
                         "error: no git command given")


if __name__ == "__main__":
    unittest.main()
