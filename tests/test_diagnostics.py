"""Tests for post-edit diagnostics injection.

These lean on the always-available Python checker (ruff if installed, else the
py_compile fallback via sys.executable), so they run anywhere. Run:
`python -m unittest tests.test_diagnostics` from the repo root.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import diagnostics  # noqa: E402


class Diagnostics(unittest.TestCase):
    def _write(self, text, suffix):
        f = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_clean_python_is_silent(self):
        path = self._write("x = 1\n", ".py")
        self.assertEqual(diagnostics.summarize(path), "")

    def test_broken_python_reports_issue(self):
        path = self._write("def f(\n", ".py")   # unclosed paren — syntax error
        out = diagnostics.summarize(path)
        self.assertIn("issue", out)
        self.assertTrue(out.startswith("\n"))    # appends onto the edit result

    def test_unknown_extension_is_silent(self):
        path = self._write("garble ][ (", ".xyzlang")
        self.assertEqual(diagnostics.summarize(path), "")

    def test_opt_out_env(self):
        path = self._write("def f(\n", ".py")
        os.environ["TWOB_NO_DIAGNOSTICS"] = "1"
        self.addCleanup(os.environ.pop, "TWOB_NO_DIAGNOSTICS", None)
        self.assertEqual(diagnostics.summarize(path), "")

    def test_summary_is_bounded(self):
        # More than MAX_ISSUES problems still yields a capped, "+N more" summary.
        many = "".join(f"import os_{i}\n" for i in range(20))  # unused imports (ruff) if present
        path = self._write(many, ".py")
        out = diagnostics.summarize(path)
        # Either clean (py_compile fallback, no ruff) or capped — never unbounded.
        if out:
            self.assertLessEqual(out.count("; ") + 1, diagnostics.MAX_ISSUES)

    def test_never_raises_on_missing_file(self):
        # A path that doesn't exist must degrade to "" not raise.
        self.assertEqual(diagnostics.summarize("/no/such/file.py"), "")

    def test_dart_parser_current_format(self):
        # `dart analyze` output (dash-separated: severity - loc - message - code).
        out = (
            "Analyzing broken.dart...\n\n"
            "  error - broken.dart:2:11 - Expected an identifier. - missing_identifier\n"
            "  warning - broken.dart:2:7 - The value of 'x' isn't used. - unused_local_variable\n\n"
            "2 issues found.\n"
        )
        issues = diagnostics._parse_dart(out)
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0], "L2: Expected an identifier.")
        self.assertIn("isn't used", issues[1])

    def test_dart_parser_bullet_format(self):
        # Older bullet-separated form (message and location order swapped).
        out = "  error • Missing semicolon. • lib/a.dart:8:3 • expected_token\n"
        issues = diagnostics._parse_dart(out)
        self.assertEqual(issues, ["L8: Missing semicolon."])


if __name__ == "__main__":
    unittest.main()
