"""Tests for tool-call argument validation (orchestrator._missing_required).

A small model sometimes emits a tool call with missing args (or an empty {}).
The dispatcher must turn that into a recoverable error string, not a KeyError
crash. Pure host-side logic. Run: `python -m unittest tests.test_dispatch_args`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.orchestrator import _missing_required  # noqa: E402


class MissingRequired(unittest.TestCase):
    def test_complete_edit_call_is_ok(self):
        self.assertEqual(
            _missing_required("edit_file", {"path": "a", "old_text": "x", "new_text": "y"}), [])

    def test_edit_missing_path_is_reported(self):
        self.assertEqual(_missing_required("edit_file", {"old_text": "x", "new_text": "y"}), ["path"])

    def test_empty_args_reports_all_required(self):
        # This is the exact live-test crash: edit_file called with {} → KeyError('path').
        self.assertEqual(_missing_required("edit_file", {}), ["path", "old_text", "new_text"])

    def test_non_dict_args_reports_all_required(self):
        self.assertEqual(_missing_required("write_file", None), ["path", "content"])
        self.assertEqual(_missing_required("read_file", "oops"), ["path"])

    def test_explicit_null_value_counts_as_missing(self):
        # {"old_text": null} would pass a bare presence check but crash downstream.
        self.assertEqual(_missing_required("edit_file", {"path": "a", "old_text": None, "new_text": "y"}),
                         ["old_text"])

    def test_empty_string_is_allowed(self):
        # An empty content/old_text is a legitimate value, not "missing".
        self.assertEqual(_missing_required("write_file", {"path": "a", "content": ""}), [])

    def test_search_requires_query_but_path_is_optional(self):
        self.assertEqual(_missing_required("search_files", {"query": "foo"}), [])
        self.assertEqual(_missing_required("search_files", {"path": "lib"}), ["query"])

    def test_tools_with_no_required_args_never_flagged(self):
        # list_files/run_git/run_command read args defensively; not in the table.
        for name in ("list_files", "run_git", "run_command", "unknown_tool"):
            self.assertEqual(_missing_required(name, {}), [])


if __name__ == "__main__":
    unittest.main()
