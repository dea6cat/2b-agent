"""Verify-loop decision helpers: fast-only tier filter + round/finish decision.
Run: `python -m unittest tests.test_verify_loop` from the repo root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator as O  # noqa: E402
from two_b.verify import CheckResult  # noqa: E402


class VerifyLoop(unittest.TestCase):
    def test_fast_filter_drops_tests_tier(self):
        checks = [("dart analyze", "fast"), ("flutter test", "tests")]
        self.assertEqual(O._verify_to_run(checks, fast_only=True), [("dart analyze", "fast")])
        self.assertEqual(O._verify_to_run(checks, fast_only=False), checks)

    def test_verdict_pass(self):
        rs = [CheckResult("dart analyze", "pass", "")]
        self.assertEqual(O._verify_verdict(rs), "pass")

    def test_verdict_fail(self):
        rs = [CheckResult("dart analyze", "pass", ""), CheckResult("flutter test", "fail", "x")]
        self.assertEqual(O._verify_verdict(rs), "fail")

    def test_verdict_cancelled_wins(self):
        rs = [CheckResult("dart analyze", "fail", "x"), CheckResult("flutter test", "cancelled", "")]
        self.assertEqual(O._verify_verdict(rs), "cancelled")

    def test_verdict_only_skips_is_pass(self):
        rs = [CheckResult("eslint .", "skipped", "")]
        self.assertEqual(O._verify_verdict(rs), "pass")


if __name__ == "__main__":
    unittest.main()
