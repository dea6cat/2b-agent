"""Tests for the unified-diff parsing behind the inline, line-numbered review
(Phase 4.2). The pure parser lives in difffmt (no rich/textual) so it's testable;
app_tui.render_diff builds the styled Text from it. Run:
`python -m unittest tests.test_render_diff`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import difffmt  # noqa: E402

_DIFF = "\n".join([
    "--- ",
    "+++ ",
    "@@ -10,3 +10,3 @@",
    " ctx before",
    "-old line",
    "+new line",
    " ctx after",
])


class DiffParsing(unittest.TestCase):
    def test_is_unified_diff(self):
        self.assertTrue(difffmt.is_unified_diff(_DIFF))
        self.assertFalse(difffmt.is_unified_diff("(full overwrite of x: 3 lines)"))
        self.assertFalse(difffmt.is_unified_diff(""))

    def test_counts_ignore_file_headers(self):
        # one +line and one -line; the +++/--- headers must not count.
        self.assertEqual(difffmt.diff_counts(_DIFF), (1, 1))

    def test_rows_track_line_numbers_and_kinds(self):
        rows = difffmt.diff_rows(_DIFF)
        # file headers + hunk header dropped; 4 content rows remain.
        self.assertEqual([r[2] for r in rows], ["ctx", "del", "add", "ctx"])
        # numbering starts at 10 for both sides (from the @@ header)
        self.assertEqual(rows[0], (10, 10, "ctx", "ctx before"))
        self.assertEqual(rows[1], (11, None, "del", "old line"))   # removed: old-side number only
        self.assertEqual(rows[2], (None, 11, "add", "new line"))   # added: new-side number only
        self.assertEqual(rows[3], (12, 12, "ctx", "ctx after"))

    def test_non_diff_has_no_rows(self):
        self.assertEqual(difffmt.diff_rows("just a preview line"), [(0, 0, "ctx", "just a preview line")])
        self.assertEqual(difffmt.diff_counts("just a preview line"), (0, 0))


if __name__ == "__main__":
    unittest.main()
