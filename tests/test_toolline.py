"""Tests for one-line tool-result summaries (Phase 4.3 / Option B). Pure logic in
`toolline`; the spinner + line rendering in app_tui isn't unit-tested. Run:
`python -m unittest tests.test_toolline`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import toolline  # noqa: E402


class ResultSummary(unittest.TestCase):
    def test_read_shows_line_count(self):
        self.assertEqual(toolline.result_summary("read_file", "a\nb\nc\n", True), "3 lines")
        self.assertEqual(toolline.result_summary("read_file", "one line", True), "1 line")

    def test_list_counts_non_blank_items(self):
        self.assertEqual(toolline.result_summary("list_files", "a.py\nb.py\n\n", True), "2 items")

    def test_write_shows_bytes(self):
        self.assertEqual(toolline.result_summary("write_file", "wrote 2610 bytes to x", True), "2610 bytes")

    def test_error_shows_exit_code(self):
        self.assertEqual(toolline.result_summary("run_git", "error: git exited 128\n…", False), "exit 128")
        self.assertEqual(toolline.result_summary("edit_file", "error: old_text not found", False), "failed")

    def test_quiet_tools_add_nothing(self):
        # search's query is already in the phrase; a clean run adds no suffix.
        self.assertEqual(toolline.result_summary("search_files", "matches…", True), "")
        self.assertEqual(toolline.result_summary("run_command", "ok output", True), "")


if __name__ == "__main__":
    unittest.main()
