"""Tests for @-file completion helpers (Phase 4.4). Pure logic in `completion`
(no rich/textual) so it's testable; app_tui wires it into the palette. Run:
`python -m unittest tests.test_completion`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import completion  # noqa: E402


class AtToken(unittest.TestCase):
    def test_active_token_after_last_at(self):
        self.assertEqual(completion.at_token("edit @lib/src/too"), "lib/src/too")
        self.assertEqual(completion.at_token("@READ"), "READ")

    def test_bare_at_is_empty_partial(self):
        self.assertEqual(completion.at_token("edit @"), "")

    def test_none_when_no_at(self):
        self.assertIsNone(completion.at_token("edit the readme"))

    def test_none_when_token_already_ended(self):
        # a space after the @token means it's finished — no active completion.
        self.assertIsNone(completion.at_token("edit @lib/foo.dart and"))

    def test_uses_the_last_at(self):
        self.assertEqual(completion.at_token("see @a.py then @b"), "b")


class RankFiles(unittest.TestCase):
    FILES = ["a2_core.dart", "lib/src/tool.dart", "lib/src/agent.dart",
             "test/tool_test.dart", "README.md"]

    def test_basename_prefix_ranks_first(self):
        # 'tool' matches tool.dart's basename (prefix) before tool_test / substring.
        r = completion.rank_files(self.FILES, "tool")
        self.assertEqual(r[0], "lib/src/tool.dart")
        self.assertIn("test/tool_test.dart", r)

    def test_path_prefix_and_substring(self):
        r = completion.rank_files(self.FILES, "lib/src")
        self.assertEqual(set(r), {"lib/src/tool.dart", "lib/src/agent.dart"})

    def test_case_insensitive(self):
        self.assertIn("README.md", completion.rank_files(self.FILES, "readme"))

    def test_empty_partial_returns_first_n(self):
        self.assertEqual(completion.rank_files(self.FILES, "", limit=2), self.FILES[:2])

    def test_limit_respected(self):
        self.assertLessEqual(len(completion.rank_files(self.FILES, "a", limit=2)), 2)

    def test_no_match_is_empty(self):
        self.assertEqual(completion.rank_files(self.FILES, "zzzz"), [])


if __name__ == "__main__":
    unittest.main()
