"""Tolerant-matching tests for do_edit_file.

The five-tool schema is frozen; these verify only the host-side matching logic —
that a small model's whitespace/indent drift still lands, while ambiguity is
always refused. Run: `python -m unittest tests.test_edit_file` from the repo root
(or `uv run python -m unittest tests.test_edit_file`).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import tools  # noqa: E402


class EditFileMatching(unittest.TestCase):
    def _edit(self, content, old_text, new_text):
        """Write content to a temp file, apply an auto-confirmed edit, return the
        tool's result string and the resulting file text."""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, newline="") as f:
            f.write(content)
            path = f.name
        try:
            result = tools.do_edit_file(path, old_text, new_text, auto_yes=True)
            with open(path, "r", newline="") as f:
                return result, f.read()
        finally:
            os.unlink(path)

    def test_exact_match_unchanged(self):
        result, out = self._edit("a = 1\nb = 2\n", "b = 2", "b = 3")
        self.assertTrue(result.startswith("edited "))
        self.assertNotIn("tolerant", result)
        self.assertEqual(out, "a = 1\nb = 3\n")

    def test_trailing_whitespace_drift(self):
        # File has a trailing space the model's old_text omits.
        result, out = self._edit("x = 1   \ny = 2\n", "x = 1\ny = 2", "x = 9\ny = 2")
        self.assertIn("whitespace-tolerant", result)
        self.assertEqual(out, "x = 9\ny = 2\n")

    def test_trailing_blank_line_in_old_text(self):
        # A small model commonly appends a stray extra newline to old_text ("…}\n\n").
        # That phantom blank line has no counterpart in the file; before the fix it
        # bounced off "old_text not found" and the model looped. It must now land.
        content = "class C {\n  int v = 0;\n}\n"
        old = "  int v = 0;\n}\n\n"          # note the extra trailing newline
        new = "  int v = 1;\n}\n"
        result, out = self._edit(content, old, new)
        self.assertTrue(result.startswith("edited "), result)
        self.assertEqual(out, "class C {\n  int v = 1;\n}\n")

    def test_line_boundary_preserved_when_new_text_drops_newline(self):
        # old_text replaces a whole line (ends in \n) but new_text omits the trailing
        # newline. Without the guard the following line merges up ("b=2\nc=3" -> "b=9c=3").
        result, out = self._edit("a = 1\nb = 2\nc = 3\n", "b = 2\n", "b = 9")
        self.assertTrue(result.startswith("edited "), result)
        self.assertEqual(out, "a = 1\nb = 9\nc = 3\n")

    def test_crlf_file_lf_old_text(self):
        # do_edit_file reads in universal-newline text mode, so a CRLF file is
        # normalized to LF before matching — the model's LF old_text lands cleanly.
        result, out = self._edit("a = 1\r\nb = 2\r\n", "a = 1\nb = 2", "a = 1\nb = 5")
        self.assertTrue(result.startswith("edited "))
        self.assertIn("b = 5", out)
        self.assertNotIn("b = 2", out)

    def test_indent_drift_reindents_new_text(self):
        # File nests the block one extra level deeper than the model's old_text.
        content = "def f():\n    if x:\n        return 1\n"
        old = "if x:\n    return 1"          # model under-indented by 4 spaces
        new = "if x:\n    return 2"
        result, out = self._edit(content, old, new)
        self.assertIn("indent-tolerant", result)
        # new_text is re-indented to the file's actual 8-space body.
        self.assertEqual(out, "def f():\n    if x:\n        return 2\n")

    def test_ambiguous_literal_refused(self):
        result, out = self._edit("p = 1\np = 1\n", "p = 1", "p = 2")
        self.assertIn("matches 2 times", result)
        self.assertEqual(out, "p = 1\np = 1\n")   # untouched

    def test_ambiguous_whitespace_refused(self):
        # Two blocks identical except trailing whitespace — genuinely ambiguous.
        content = "q = 1   \nq = 1\n"
        result, out = self._edit(content, "q = 1", "q = 2")
        self.assertIn("matches", result)
        self.assertEqual(out, content)           # untouched

    def test_absent_refused(self):
        result, out = self._edit("only = 1\n", "nowhere = 9", "x = 0")
        self.assertIn("not found", result)
        self.assertEqual(out, "only = 1\n")

    def test_not_found_error_hints_at_nearest_line(self):
        # A genuine mismatch (typo, not just whitespace) that the tolerant tiers can't
        # absorb should point the model at the closest real line instead of a dead end.
        content = "def run():\n    total = compute(x)\n    return total\n"
        result, out = self._edit(content, "    totl = compute(x)", "    total = compute(y)")
        self.assertIn("old_text not found", result)
        self.assertIn("closest line in the file is line 2", result.lower())
        self.assertIn("total = compute(x)", result)
        self.assertEqual(out, content)   # file left unchanged on failure

    def test_missing_file(self):
        result = tools.do_edit_file("/no/such/path/xyz.py", "a", "b", auto_yes=True)
        self.assertTrue(result.startswith("error: no such file"))


if __name__ == "__main__":
    unittest.main()
