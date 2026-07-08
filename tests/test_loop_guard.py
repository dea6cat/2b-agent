"""Tests for the graduated tool-call loop guard (orchestrator._LoopGuard).

warn -> veto -> breaker, keyed on the tool result with volatile fields stripped.
Pure host-side logic — no model needed. Run:
`python -m unittest tests.test_loop_guard` from the repo root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402
from two_b.conversation import Message, ToolCall  # noqa: E402
from two_b.orchestrator import _LoopGuard, _strip_volatile, EventType  # noqa: E402
from two_b.providers.base import ProviderResponse  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class Ladder(unittest.TestCase):
    def test_warn_then_veto_then_breaker(self):
        g = _LoopGuard(warn_at=3, veto_at=5, breaker_vetoes=3)
        args = {"path": "a.dart", "old_text": "x", "new_text": "y"}
        err = "error: old_text not found in file"
        self.assertEqual(g.record("edit_file", args, err), "")        # 1
        self.assertEqual(g.record("edit_file", args, err), "")        # 2
        self.assertEqual(g.record("edit_file", args, err), "warn")    # 3 -> warn
        self.assertEqual(g.record("edit_file", args, err), "")        # 4 (already warned)
        self.assertEqual(g.record("edit_file", args, err), "veto")    # 5 -> veto #1
        self.assertEqual(g.record("edit_file", args, err), "veto")    # 6 -> veto #2
        self.assertEqual(g.record("edit_file", args, err), "breaker") # 7 -> veto #3 -> breaker

    def test_warn_fires_only_once_per_signature(self):
        g = _LoopGuard(warn_at=2, veto_at=99)
        a = {"path": "a"}
        self.assertEqual(g.record("read_file", a, "ok"), "")
        self.assertEqual(g.record("read_file", a, "ok"), "warn")
        self.assertEqual(g.record("read_file", a, "ok"), "")   # not warned again

    def test_vetoes_across_different_signatures_still_reach_breaker(self):
        # The model thrashes: loops on A to a veto, switches to B and loops to a veto,
        # then C — three vetoes total should break even though no single call hit it thrice.
        g = _LoopGuard(warn_at=3, veto_at=3, breaker_vetoes=3)
        seen = []
        for path in ("a", "b", "c"):
            v = ""
            for _ in range(3):
                v = g.record("edit_file", {"path": path}, "error: not found")
            seen.append(v)
        # a -> veto, b -> veto, c -> breaker (3rd veto)
        self.assertEqual(seen, ["veto", "veto", "breaker"])


class Signature(unittest.TestCase):
    def test_distinct_calls_do_not_trip(self):
        g = _LoopGuard(warn_at=3, veto_at=5)
        for i in range(10):
            self.assertEqual(g.record("read_file", {"path": f"f{i}.py"}, "ok"), "")

    def test_same_call_different_result_is_progress(self):
        g = _LoopGuard(warn_at=2, veto_at=3)
        args = {"path": "a", "old_text": "x", "new_text": "y"}
        self.assertEqual(g.record("edit_file", args, "error: not found"), "")
        self.assertEqual(g.record("edit_file", args, "edited a"), "")   # different result

    def test_same_command_different_failures_is_not_a_loop(self):
        # A model iterating on tests reruns the SAME command; each run fails differently.
        # Distinct failure text (test names) must not collapse into one signature.
        g = _LoopGuard(warn_at=3, veto_at=5)
        args = {"command": "pytest"}
        for i in range(6):
            v = g.record("run_command", args, f"error: command exited 1\nFAILED test_{i}")
            self.assertEqual(v, "", f"run {i} should not trip (distinct failures)")

    def test_edit_to_different_files_does_not_collide(self):
        g = _LoopGuard(warn_at=3, veto_at=5)
        big = "x" * 400
        for i in range(6):
            v = g.record("edit_file", {"path": f"file_{i}.py", "old_text": big, "new_text": big},
                         "error: old_text not found in file")
            self.assertEqual(v, "", f"edit to file_{i} should not trip (distinct files)")

    def test_evicted_signature_gets_a_fresh_warn(self):
        g = _LoopGuard(window=3, warn_at=2, veto_at=3)
        a = {"path": "a"}
        self.assertEqual(g.record("edit_file", a, "err"), "")
        self.assertEqual(g.record("edit_file", a, "err"), "warn")    # warned once
        g.record("read_file", {"p": 1}, "ok")                        # push it out of the window
        g.record("read_file", {"p": 2}, "ok")
        g.record("read_file", {"p": 3}, "ok")
        self.assertEqual(g.record("edit_file", a, "err"), "")        # fresh streak, count 1
        self.assertEqual(g.record("edit_file", a, "err"), "warn")    # warned again, not escalated

    def test_window_forgets_old_calls(self):
        g = _LoopGuard(window=3, warn_at=3, veto_at=3)
        args = {"path": "a"}
        g.record("edit_file", args, "err")
        g.record("read_file", {"p": 1}, "ok")
        g.record("read_file", {"p": 2}, "ok")
        g.record("read_file", {"p": 3}, "ok")   # first edit_file now evicted
        self.assertEqual(g.record("edit_file", args, "err"), "")


class VolatileStripping(unittest.TestCase):
    def test_timestamp_only_difference_still_trips(self):
        # A test rerun whose ONLY difference is its duration used to slip past a
        # whole-result hash; stripping the duration makes the stuck repeat trip.
        g = _LoopGuard(warn_at=2, veto_at=99)
        a = {"command": "pytest"}
        self.assertEqual(g.record("run_command", a, "1 failed, 0 passed in 1.23s"), "")
        self.assertEqual(g.record("run_command", a, "1 failed, 0 passed in 8.09s"), "warn")

    def test_clock_and_hash_stripped(self):
        self.assertEqual(_strip_volatile("built at 12:34:56"), _strip_volatile("built at 09:01:02"))
        self.assertEqual(_strip_volatile("commit a1b2c3d4e5f6 ok"), _strip_volatile("commit f6e5d4c3b2a1 ok"))

    def test_distinct_text_not_collapsed(self):
        # Different real content (a test name) must remain distinct after stripping.
        self.assertNotEqual(_strip_volatile("FAILED test_alpha"), _strip_volatile("FAILED test_beta"))


class _StuckProvider:
    """Always makes the same failing tool call — drives the loop guard to its breaker."""
    name, api_key = "fake", "x"

    def is_available(self):
        return True

    def list_models(self):
        return ["m"]

    def stream(self, conv, model, tools, on_text, *, cancel=None):
        return ProviderResponse(message=Message.assistant(
            tool_calls=[ToolCall.new("read_file", {"path": "nope.txt"})]), raw={})


class _BatchLooper:
    """Always requests the same 2-read batch, so the guard breaks on the FIRST call of a
    multi-call batch — the case where a mid-batch break would orphan the later call."""
    name, api_key = "fake", "x"

    def __init__(self):
        self.last_conv = None

    def is_available(self):
        return True

    def list_models(self):
        return ["m"]

    def stream(self, conv, model, tools, on_text, *, cancel=None):
        self.last_conv = conv
        return ProviderResponse(message=Message.assistant(tool_calls=[
            ToolCall.new("read_file", {"path": "a.txt"}),
            ToolCall.new("read_file", {"path": "b.txt"})]), raw={})


class BreakerEndsGracefully(unittest.TestCase):
    def test_breaker_mid_batch_keeps_tool_call_result_pairing(self):
        # Regression: a breaker on a non-last call in a batch must NOT orphan later calls —
        # every tool_call must still get a tool_result, or the next provider call is malformed.
        import shutil
        import tempfile
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        cwd = os.getcwd()
        os.chdir(proj)
        self.addCleanup(os.chdir, cwd)
        for name in ("a.txt", "b.txt"):
            with open(os.path.join(proj, name), "w") as f:
                f.write(name + " body\n")
        os.environ["TWOB_NO_TRIM"] = "1"                 # inspect the full, untrimmed conv
        self.addCleanup(os.environ.pop, "TWOB_NO_TRIM", None)
        provider = _BatchLooper()
        s = Session(default_model="fake:m")
        t = Task(description="read both")
        events = []
        orchestrator.run_task(s, t, events.append, {"fake": provider})
        types = [e.type for e in events]
        self.assertIn(EventType.TASK_DONE, types)
        self.assertNotIn(EventType.TASK_ERROR, types)
        # Every assistant turn that requested N tool calls is followed by N tool results.
        msgs = provider.last_conv.messages
        for i, m in enumerate(msgs):
            if m.tool_calls:
                self.assertLess(i + 1, len(msgs))
                nxt = msgs[i + 1]
                self.assertEqual(len(nxt.tool_results), len(m.tool_calls),
                                 f"turn {i}: {len(m.tool_calls)} calls but {len(nxt.tool_results)} results")

    def test_stuck_loop_ends_in_a_final_answer_not_an_error(self):
        # The breaker must degrade to a graceful final answer (TASK_DONE), never a hard
        # error, and never run out to MAX_TURNS.
        s = Session(default_model="fake:m")
        t = Task(description="read nope.txt")
        events = []
        orchestrator.run_task(s, t, events.append, {"fake": _StuckProvider()})
        types = [e.type for e in events]
        self.assertIn(EventType.TASK_DONE, types)
        self.assertNotIn(EventType.TASK_ERROR, types)
        logs = [e.payload.get("text", "") for e in events if e.type == EventType.LOG]
        self.assertTrue(any("Loop breaker" in m for m in logs), logs)
        turns = types.count(EventType.TURN_START)
        self.assertLess(turns, orchestrator.MAX_TURNS, f"should break early, not run {turns} turns")


if __name__ == "__main__":
    unittest.main()
