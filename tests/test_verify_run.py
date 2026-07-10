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

    def test_skipped_check_does_not_fire_on_start(self):
        # A missing binary is skipped before running — it must not emit a spurious "running…".
        seen = []
        verify.run_checks([("definitely-no-such-bin-xyz build", "fast")], on_start=seen.append)
        self.assertEqual(seen, [])


class Truncate(unittest.TestCase):
    def test_under_cap_is_identity(self):
        s = "x" * 100
        self.assertEqual(verify._truncate(s, cap=4000), s)

    def test_never_longer_than_input_near_boundary(self):
        # The elision marker must not inflate an input only slightly over the cap.
        for extra in range(1, 40):
            s = "y" * (4000 + extra)
            self.assertLessEqual(len(verify._truncate(s, cap=4000)), len(s))

    def test_large_input_keeps_head_and_tail(self):
        s = "HEAD" + ("m" * 20000) + "TAIL"
        out = verify._truncate(s, cap=4000)
        self.assertLess(len(out), len(s))
        self.assertTrue(out.startswith("HEAD"))
        self.assertTrue(out.endswith("TAIL"))
        self.assertIn("truncated", out)


if __name__ == "__main__":
    unittest.main()
