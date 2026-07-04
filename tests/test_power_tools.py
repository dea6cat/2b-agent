"""Tests for P19 power-tool helpers: /tool invocation parsing and the inline-confirmation
risk classifier. Pure logic (the TUI wiring that calls these lives in app_tui). Run:
`python -m unittest tests.test_power_tools`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import commands  # noqa: E402


class ParseToolInvocation(unittest.TestCase):
    def test_key_value_args(self):
        name, args = commands.parse_tool_invocation("read_file path=lib/a.dart")
        self.assertEqual(name, "read_file")
        self.assertEqual(args, {"path": "lib/a.dart"})

    def test_quoted_value_with_spaces(self):
        name, args = commands.parse_tool_invocation('search_files query="foo bar" path=lib')
        self.assertEqual(name, "search_files")
        self.assertEqual(args, {"query": "foo bar", "path": "lib"})

    def test_json_args(self):
        name, args = commands.parse_tool_invocation('edit_file {"path": "a", "old_text": "x", "new_text": "y"}')
        self.assertEqual(name, "edit_file")
        self.assertEqual(args["new_text"], "y")

    def test_no_args_is_empty_dict(self):
        self.assertEqual(commands.parse_tool_invocation("list_files"), ("list_files", {}))

    def test_unknown_tool_rejected(self):
        name, err = commands.parse_tool_invocation("delegate task=foo")
        self.assertIsNone(name)
        self.assertIn("not a directly-invocable tool", err)

    def test_empty_is_usage(self):
        name, err = commands.parse_tool_invocation("")
        self.assertIsNone(name)
        self.assertIn("Usage", err)

    def test_bad_json_reports_error(self):
        name, err = commands.parse_tool_invocation("read_file {not json}")
        self.assertIsNone(name)
        self.assertIn("JSON", err)

    def test_json_must_be_object(self):
        name, err = commands.parse_tool_invocation('read_file [1,2]')
        self.assertIsNone(name)
        self.assertIn("object", err)

    def test_bare_token_without_equals_rejected(self):
        name, err = commands.parse_tool_invocation("read_file lib/a.dart")
        self.assertIsNone(name)
        self.assertIn("key=value", err)


class ConfirmationRisk(unittest.TestCase):
    _DIFF = "\n".join(["--- ", "+++ ", "@@ -1,2 +1,3 @@", " keep", "-old", "+new1", "+new2"])

    def test_edit_is_write_with_line_counts(self):
        risk, impact = commands.confirmation_risk("edit_file", self._DIFF)
        self.assertEqual(risk, "write")
        self.assertEqual(impact, "+2/-1 lines")

    def test_write_file_full_overwrite_summary(self):
        risk, impact = commands.confirmation_risk("write_file", "(full overwrite of x: 3 lines)")
        self.assertEqual(risk, "write")
        self.assertIn("full overwrite", impact)

    def test_run_command_is_execute(self):
        risk, impact = commands.confirmation_risk("run_command", "$ dart test")
        self.assertEqual(risk, "execute")
        self.assertEqual(impact, "dart test")

    def test_rm_command_is_delete(self):
        risk, _ = commands.confirmation_risk("run_command", "$ rm -rf build/")
        self.assertEqual(risk, "delete")

    def test_git_rm_is_delete(self):
        risk, _ = commands.confirmation_risk("run_git", "$ git rm old.dart")
        self.assertEqual(risk, "delete")

    def test_git_branch_delete_is_delete(self):
        risk, _ = commands.confirmation_risk("run_git", "$ branch -D feature")
        self.assertEqual(risk, "delete")

    def test_plain_git_is_execute(self):
        risk, _ = commands.confirmation_risk("run_git", "$ commit -m msg")
        self.assertEqual(risk, "execute")

    def test_echo_reboot_is_not_delete(self):
        # A benign command mentioning a scary word must not be misclassified.
        risk, _ = commands.confirmation_risk("run_command", "$ echo rm is dangerous")
        self.assertEqual(risk, "execute")

    def test_more_destructive_commands_are_delete(self):
        for cmd in ("$ find . -name '*.tmp' -delete", "$ git clean -fd", "$ dd if=/dev/zero of=x",
                    "$ truncate -s 0 log.txt", "$ git branch -D feature"):
            risk, _ = commands.confirmation_risk("run_command", cmd)
            self.assertEqual(risk, "delete", cmd)

    def test_delete_detection_anchored_on_command_token(self):
        # 'rm' as an argument, not the command, is not a delete.
        risk, _ = commands.confirmation_risk("run_command", "$ grep rm README.md")
        self.assertEqual(risk, "execute")


if __name__ == "__main__":
    unittest.main()
