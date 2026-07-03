"""run_git is git-only (no shell). A model that tries to chain/pipe/redirect should
get a clear, recoverable error, not git's cryptic exit 128. Found during a live run
where the model sent 'add … && diff HEAD …'. Run: `python -m unittest tests.test_run_git_shell`.
"""
import os
import shlex
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import tools  # noqa: E402


class GitShellSyntax(unittest.TestCase):
    def test_detects_chaining_and_redirection(self):
        for s in ("add x && diff y", "status || echo no", "log | head", "a ; b",
                  "diff HEAD 2>/dev/null", "show > out.txt", "cat < in", "diff >> log"):
            self.assertTrue(tools._git_shell_syntax(shlex.split(s)), s)

    def test_quoted_operator_in_message_is_not_flagged(self):
        # The whole point: an operator INSIDE a quoted arg is one token, not chaining.
        for s in ('commit -m "fix a && b"', 'commit -m "pipe | in message"',
                  'commit -m "redirect > here"', "commit -m 'a; b'"):
            self.assertFalse(tools._git_shell_syntax(shlex.split(s)), s)

    def test_ordinary_git_commands_are_not_flagged(self):
        for s in ("status", "add -A", "diff --cached", "commit -m fix", "log --oneline -5",
                  "diff HEAD -- lib/src/tool.dart"):
            self.assertFalse(tools._git_shell_syntax(shlex.split(s)), s)

    def test_do_run_git_returns_recoverable_error(self):
        # Early return before git ever runs — no repo needed.
        out = tools.do_run_git("add lib/src/tool.dart && diff HEAD -- lib/src/tool.dart")
        self.assertIn("no shell operators", out)
        self.assertIn("run_git", out)

    def test_has_shell_syntax_gates_confirmation(self):
        # Used by the orchestrator to reject shell-chained git BEFORE prompting.
        self.assertTrue(tools.has_shell_syntax("add -A && commit -m x"))
        self.assertTrue(tools.has_shell_syntax("diff HEAD 2>/dev/null | head"))
        self.assertFalse(tools.has_shell_syntax("commit -m 'a && b'"))   # quoted -> fine
        self.assertFalse(tools.has_shell_syntax("status"))
        self.assertFalse(tools.has_shell_syntax(""))


if __name__ == "__main__":
    unittest.main()
