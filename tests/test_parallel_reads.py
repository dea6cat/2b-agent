"""Tests for P3 parallel read batching.

A batch of side-effect-free reads runs concurrently (order preserved); mutating
or confirmation-gated calls stay serialized. Host-side; the frozen schema is
untouched. Run: `python -m unittest tests.test_parallel_reads`.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator, tools  # noqa: E402
from two_b.conversation import Message, ToolCall  # noqa: E402
from two_b.orchestrator import EventType  # noqa: E402
from two_b.providers.base import ProviderResponse  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class Classify(unittest.TestCase):
    def test_read_tools_are_parallel(self):
        for name in ("read_file", "list_files", "search_files"):
            self.assertTrue(orchestrator._is_parallel_read(name, {"path": "x"}), name)

    def test_git_is_never_parallel(self):
        # Even read-only git is excluded — concurrent git can collide on .git/index.lock.
        self.assertFalse(orchestrator._is_parallel_read("run_git", {"args": "status"}))
        self.assertFalse(orchestrator._is_parallel_read("run_git", {"args": "log --oneline"}))
        self.assertFalse(orchestrator._is_parallel_read("run_git", {"args": "commit -m x"}))

    def test_mutating_and_gated_tools_are_not_parallel(self):
        for name in ("edit_file", "write_file", "run_command", "some_mcp_tool"):
            self.assertFalse(orchestrator._is_parallel_read(name, {}), name)


class _Base(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(self.proj)
        self.addCleanup(os.chdir, cwd)
        self.addCleanup(shutil.rmtree, self.proj, ignore_errors=True)

    def _write(self, rel, content):
        with open(os.path.join(self.proj, rel), "w") as f:
            f.write(content)


class ConcurrentRuns(_Base):
    def test_results_preserve_call_order(self):
        self._write("a.txt", "AAA\n")
        self._write("b.txt", "BBB\n")
        self._write("c.txt", "CCC\n")
        s, t = Session(default_model="m"), Task(description="t")
        calls = [ToolCall.new("read_file", {"path": p}) for p in ("a.txt", "b.txt", "c.txt")]
        out = orchestrator._run_reads_concurrently(s, t, calls, None)
        self.assertEqual(len(out), 3)
        self.assertIn("AAA", out[0]); self.assertIn("BBB", out[1]); self.assertIn("CCC", out[2])

    def test_runs_actually_concurrent(self):
        # Four reads that each block 0.2s finish in ~0.2s concurrently, not ~0.8s serially.
        self._write("a.txt", "x\n")
        s, t = Session(default_model="m"), Task(description="t")
        calls = [ToolCall.new("read_file", {"path": "a.txt"}) for _ in range(4)]
        orig = tools.do_read_file
        tools.do_read_file = lambda *a, **k: (time.sleep(0.2), orig(*a, **k))[1]
        try:
            t0 = time.monotonic()
            out = orchestrator._run_reads_concurrently(s, t, calls, None)
            elapsed = time.monotonic() - t0
        finally:
            tools.do_read_file = orig
        self.assertEqual(len(out), 4)
        self.assertLess(elapsed, 0.5, f"reads did not run concurrently (took {elapsed:.2f}s)")

    def test_identical_reads_in_a_batch_are_deduped(self):
        # A repeated identical read does the I/O once but still yields a result for every
        # position (so the batch stays 1:1 with the model's calls).
        self._write("a.txt", "AAA\n")
        s, t = Session(default_model="m"), Task(description="t")
        calls = [ToolCall.new("read_file", {"path": "a.txt"}) for _ in range(3)]
        n = {"c": 0}
        orig = tools.do_read_file

        def counting(path, **k):
            n["c"] += 1
            return orig(path, **k)
        tools.do_read_file = counting
        try:
            out = orchestrator._run_reads_concurrently(s, t, calls, None)
        finally:
            tools.do_read_file = orig
        self.assertEqual(len(out), 3)
        self.assertTrue(all("AAA" in o for o in out))
        self.assertEqual(n["c"], 1)   # deduped: one actual read for three identical calls

    def test_one_failing_read_does_not_sink_the_batch(self):
        self._write("a.txt", "AAA\n")
        s, t = Session(default_model="m"), Task(description="t")
        calls = [ToolCall.new("read_file", {"path": "a.txt"}),
                 ToolCall.new("read_file", {"path": "boom"})]
        orig = tools.do_read_file

        def flaky(path, **k):
            if path == "boom":
                raise RuntimeError("kaboom")
            return orig(path, **k)
        tools.do_read_file = flaky
        try:
            out = orchestrator._run_reads_concurrently(s, t, calls, None)
        finally:
            tools.do_read_file = orig
        self.assertIn("AAA", out[0])
        self.assertTrue(out[1].startswith("error:"))
        self.assertIn("kaboom", out[1])


class _Scripted:
    """Fake provider that returns pre-built responses turn by turn (cloud-shaped)."""
    name = "fake"
    api_key = "x"

    def __init__(self, responses):
        self._responses, self._i = list(responses), 0

    def is_available(self):
        return True

    def list_models(self):
        return ["m"]

    def stream(self, conv, model, tools_, on_text, *, cancel=None):
        r = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        if r.message.text:
            on_text(r.message.text)
        return r


class Integration(_Base):
    def test_batch_of_reads_flows_through_run_task_in_order(self):
        self._write("a.txt", "AAA\n")
        self._write("b.txt", "BBB\n")
        provider = _Scripted([
            ProviderResponse(message=Message.assistant(tool_calls=[
                ToolCall.new("read_file", {"path": "a.txt"}),
                ToolCall.new("read_file", {"path": "b.txt"})]), raw={}),
            ProviderResponse(message=Message.assistant(text="done"), raw={}),
        ])
        s, t = Session(default_model="fake:m"), Task(description="read both files")
        events = []
        orchestrator.run_task(s, t, events.append, {"fake": provider})
        types = [e.type for e in events]
        self.assertIn(EventType.TASK_DONE, types)
        self.assertNotIn(EventType.TASK_ERROR, types)
        res = [e.payload["result"] for e in events if e.type == EventType.TOOL_CALL_RESULT]
        self.assertEqual(len(res), 2)
        self.assertIn("AAA", res[0])   # order preserved: a before b
        self.assertIn("BBB", res[1])


if __name__ == "__main__":
    unittest.main()
