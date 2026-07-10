"""verify.run_checks — host-run checks with pass/fail/skipped/cancelled + progress.
Run: `python -m unittest tests.test_verify_run` from the repo root.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import verify  # noqa: E402


class RunChecks(unittest.TestCase):
    def test_pass(self):
        r = verify.run_checks([("sh -c 'exit 0'", "fast")])
        self.assertEqual([(x.cmd, x.status) for x in r], [("sh -c 'exit 0'", "pass")])

    def test_fail_captures_output(self):
        r = verify.run_checks([("sh -c 'echo boom; exit 1'", "tests")])
        self.assertEqual(r[0].status, "fail")
        self.assertIn("boom", r[0].output)

    def test_missing_binary_skipped_not_failed(self):
        r = verify.run_checks([("definitely-no-such-bin-xyz build", "fast")])
        self.assertEqual(r[0].status, "skipped")

    def test_on_start_fires_per_command(self):
        seen = []
        verify.run_checks([("sh -c 'exit 0'", "fast")], on_start=seen.append)
        self.assertEqual(seen, ["sh -c 'exit 0'"])

    def test_preset_cancel_returns_cancelled(self):
        ev = threading.Event()
        ev.set()
        r = verify.run_checks([("sh -c 'exit 0'", "fast")], cancel=ev)
        self.assertEqual(r[0].status, "cancelled")


if __name__ == "__main__":
    unittest.main()
