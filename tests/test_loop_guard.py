"""Tests for the tool-call loop guard (orchestrator._LoopGuard).

Pure host-side logic — no model needed. Run:
`python -m unittest tests.test_loop_guard` from the repo root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.orchestrator import _LoopGuard  # noqa: E402


class LoopGuard(unittest.TestCase):
    def test_repeated_identical_call_nudges_then_stops(self):
        g = _LoopGuard(nudge_at=3, stop_at=5)
        args = {"path": "a.dart", "old_text": "x", "new_text": "y"}
        err = "error: old_text not found in file"
        self.assertEqual(g.record("edit_file", args, err), "")   # 1
        self.assertEqual(g.record("edit_file", args, err), "")   # 2
        self.assertEqual(g.record("edit_file", args, err), "nudge")  # 3 -> nudge
        self.assertEqual(g.record("edit_file", args, err), "")   # 4 (already nudged)
        self.assertEqual(g.record("edit_file", args, err), "stop")   # 5 -> stop

    def test_nudge_fires_only_once_per_signature(self):
        g = _LoopGuard(nudge_at=2, stop_at=99)
        a = {"path": "a"}
        self.assertEqual(g.record("read_file", a, "ok"), "")
        self.assertEqual(g.record("read_file", a, "ok"), "nudge")
        self.assertEqual(g.record("read_file", a, "ok"), "")   # not nudged again

    def test_distinct_calls_do_not_trip(self):
        g = _LoopGuard(nudge_at=3, stop_at=5)
        for i in range(10):
            self.assertEqual(g.record("read_file", {"path": f"f{i}.py"}, "ok"), "")

    def test_same_call_different_result_is_progress(self):
        # A retry that produces a *different* result (e.g. edit finally succeeds)
        # is not a loop — the result is part of the signature.
        g = _LoopGuard(nudge_at=2, stop_at=3)
        args = {"path": "a", "old_text": "x", "new_text": "y"}
        self.assertEqual(g.record("edit_file", args, "error: not found"), "")
        self.assertEqual(g.record("edit_file", args, "edited a"), "")   # different result

    def test_same_command_different_failures_is_not_a_loop(self):
        # A model iterating on tests reruns the SAME command; each run fails
        # differently. run_command/run_git both prefix failures with a constant
        # "error: command exited N" line — the guard must key off the whole output,
        # not line 1, or it would falsely stop an active fix→rerun loop.
        g = _LoopGuard(nudge_at=3, stop_at=5)
        args = {"command": "pytest"}
        for i in range(6):
            v = g.record("run_command", args, f"error: command exited 1\nFAILED test_{i}")
            self.assertEqual(v, "", f"run {i} should not trip (distinct failures)")

    def test_edit_to_different_files_does_not_collide(self):
        # Large old/new text with different paths must not hash to the same signature
        # even if the arg string is truncated (path is placed first).
        g = _LoopGuard(nudge_at=3, stop_at=5)
        big = "x" * 400
        for i in range(6):
            path = f"file_{i}.py"
            v = g.record("edit_file", {"path": path, "old_text": big, "new_text": big},
                         "error: old_text not found in file")
            self.assertEqual(v, "", f"edit to {path} should not trip (distinct files)")

    def test_evicted_signature_gets_a_fresh_nudge(self):
        # A signature that ages out of the window and later recurs is warned again,
        # not silently escalated straight to stop.
        g = _LoopGuard(window=3, nudge_at=2, stop_at=3)
        a = {"path": "a"}
        self.assertEqual(g.record("edit_file", a, "err"), "")
        self.assertEqual(g.record("edit_file", a, "err"), "nudge")   # nudged once
        # Push it out of the 3-wide window.
        g.record("read_file", {"p": 1}, "ok")
        g.record("read_file", {"p": 2}, "ok")
        g.record("read_file", {"p": 3}, "ok")
        self.assertEqual(g.record("edit_file", a, "err"), "")        # fresh streak, count 1
        self.assertEqual(g.record("edit_file", a, "err"), "nudge")   # nudged again, not stopped

    def test_window_forgets_old_calls(self):
        # Outside the rolling window, an old repeat no longer counts toward the total.
        g = _LoopGuard(window=3, nudge_at=3, stop_at=3)
        args = {"path": "a"}
        g.record("edit_file", args, "err")
        g.record("read_file", {"p": 1}, "ok")
        g.record("read_file", {"p": 2}, "ok")
        g.record("read_file", {"p": 3}, "ok")   # first edit_file now evicted
        self.assertEqual(g.record("edit_file", args, "err"), "")  # only 1 in window


if __name__ == "__main__":
    unittest.main()
