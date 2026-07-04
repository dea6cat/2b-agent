"""Tests for P25 steer (interrupt-and-redirect).

A message the user sends while a turn runs is buffered on the task and folded into
the last tool result the model reads next — so it course-corrects without losing
in-flight work. Host-side; the frozen schema is untouched.
Run: `python -m unittest tests.test_steer`.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402
from two_b.conversation import Message, Role, ToolCall  # noqa: E402
from two_b.providers.base import ProviderResponse  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class Buffer(unittest.TestCase):
    def test_push_take_roundtrip_and_clear(self):
        t = Task(description="x")
        self.assertEqual(t.take_steer(), "")
        t.push_steer("do it differently")
        t.push_steer("and also this")
        self.assertEqual(t.take_steer(), "do it differently\nand also this")
        self.assertEqual(t.take_steer(), "")            # drained

    def test_blank_steer_ignored(self):
        t = Task(description="x")
        t.push_steer("   ")
        self.assertEqual(t.take_steer(), "")

    def test_clear_drops_pending(self):
        t = Task(description="x")
        t.push_steer("redirect")
        t.clear_steer()
        self.assertEqual(t.take_steer(), "")


class _Base(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(self.proj)
        self.addCleanup(os.chdir, cwd)
        self.addCleanup(shutil.rmtree, self.proj, ignore_errors=True)
        with open(os.path.join(self.proj, "a.txt"), "w") as f:
            f.write("hello\n")


def _last_tool_result(conv):
    for m in reversed(conv.messages):
        if m.tool_results:
            return m.tool_results[-1].content
    return None


class Consumption(_Base):
    def test_steer_typed_mid_turn_lands_in_next_tool_result(self):
        # The provider pushes a steer while its first turn runs (as if the user typed it),
        # makes a read call, then on turn 2 captures the conversation it receives.
        captured = {}

        class P:
            name, api_key = "fake", "x"
            def __init__(self): self.i = 0
            def is_available(self): return True
            def list_models(self): return ["m"]
            def stream(self, conv, model, tools, on_text):
                self.i += 1
                if self.i == 1:
                    task.push_steer("actually, focus on error handling")
                    return ProviderResponse(message=Message.assistant(
                        tool_calls=[ToolCall.new("read_file", {"path": "a.txt"})]), raw={})
                captured["conv"] = conv
                return ProviderResponse(message=Message.assistant(text="ok"), raw={})

        s = Session(default_model="fake:m")
        task = Task(description="read a.txt")
        orchestrator.run_task(s, task, lambda e: None, {"fake": P()})
        result = _last_tool_result(captured["conv"])
        self.assertIsNotNone(result)
        self.assertIn("hello", result)                         # the real tool output is preserved
        self.assertIn("[user steer", result)                   # the redirect rode along
        self.assertIn("focus on error handling", result)
        self.assertEqual(task.take_steer(), "")                # consumed, not left dangling

    def test_steer_with_no_tool_result_stays_buffered(self):
        # If the turn ends with a final answer (no tool call), there's no result to carry
        # the steer — it stays buffered for the UI to resubmit as a new task.
        class P:
            name, api_key = "fake", "x"
            def is_available(self): return True
            def list_models(self): return ["m"]
            def stream(self, conv, model, tools, on_text):
                on_text("done")
                return ProviderResponse(message=Message.assistant(text="done"), raw={})

        s = Session(default_model="fake:m")
        task = Task(description="just answer")
        task.push_steer("wait, do X instead")
        orchestrator.run_task(s, task, lambda e: None, {"fake": P()})
        self.assertEqual(task.take_steer(), "wait, do X instead")   # re-stashed, not lost


if __name__ == "__main__":
    unittest.main()
