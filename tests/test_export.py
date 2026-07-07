"""Tests for /export — dump the whole session (tool calls + results included) to Markdown.

Pure host-side: exercises commands._export / _render_session_md over real Conversation
objects with a fake app. No textual needed.
Run: `python -m unittest tests.test_export`.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import commands  # noqa: E402
from two_b.conversation import Conversation, Message, ToolCall, ToolResult  # noqa: E402
from two_b.session import Session  # noqa: E402


class _FakeUI:
    def __init__(self): self.out = []
    def print(self, *a): self.out.append(" ".join(str(x) for x in a))


class _FakeApp:
    def __init__(self, session): self.session = session; self.ui = _FakeUI()


def _conv_with_tool_call():
    c = Conversation(system_prompt="sys")
    c.append(Message.user("read the xlsx and pull the data"))
    c.append(Message.assistant(
        text="I'll inspect it.", thinking="check the file type first",
        tool_calls=[ToolCall.new("run_command", {"command": "file report.xlsx"}, id="c1")]))
    c.append(Message.results([ToolResult(tool_call_id="c1", content="report.xlsx: Excel 2007+")]))
    c.append(Message.assistant(text="It has 3 columns."))
    return c


class Export(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, ignore_errors=True)

    def _session(self):
        return Session(default_model="google:gemini-2.5-pro", cwd=self.dir)

    def test_tool_calls_results_and_thinking_are_all_present(self):
        s = self._session()
        s.add_task("read the xlsx").conversation = _conv_with_tool_call()
        app = _FakeApp(s)
        commands._export("out.md", app)
        md = open(os.path.join(self.dir, "out.md"), encoding="utf-8").read()
        self.assertIn("## You", md)
        self.assertIn("read the xlsx and pull the data", md)
        self.assertIn("**⚙ run_command**", md)
        self.assertIn('{"command": "file report.xlsx"}', md)   # raw args, lossless
        self.assertIn("→ result:", md)
        self.assertIn("report.xlsx: Excel 2007+", md)
        self.assertIn("_thinking:_", md)
        self.assertIn("check the file type first", md)
        self.assertIn("It has 3 columns.", md)

    def test_error_result_is_flagged(self):
        s = self._session()
        c = Conversation(system_prompt="sys")
        c.append(Message.assistant(tool_calls=[ToolCall.new("run_command", {"command": "nope"}, id="c1")]))
        c.append(Message.results([ToolResult(tool_call_id="c1", content="boom", is_error=True)]))
        s.add_task("x").conversation = c
        commands._export("out.md", _FakeApp(s))
        md = open(os.path.join(self.dir, "out.md"), encoding="utf-8").read()
        self.assertIn("⚠ error:", md)
        self.assertIn("boom", md)

    def test_shared_continuity_thread_is_not_duplicated(self):
        s = self._session()
        conv = _conv_with_tool_call()
        s.add_task("q1").conversation = conv
        s.add_task("q2").conversation = conv          # same object (continuity thread)
        self.assertEqual(len(commands._session_conversations(s)), 1)
        commands._export("out.md", _FakeApp(s))
        md = open(os.path.join(self.dir, "out.md"), encoding="utf-8").read()
        self.assertIn("1 conversation(s)", md)
        self.assertEqual(md.count("read the xlsx and pull the data"), 1)

    def test_detached_conversations_both_appear(self):
        s = self._session()
        s.add_task("first").conversation = _conv_with_tool_call()
        c2 = Conversation(system_prompt="sys")
        c2.append(Message.user("a totally separate question"))
        s.add_task("second").conversation = c2
        commands._export("out.md", _FakeApp(s))
        md = open(os.path.join(self.dir, "out.md"), encoding="utf-8").read()
        self.assertIn("2 conversation(s)", md)
        self.assertIn("conversation 2", md)
        self.assertIn("read the xlsx and pull the data", md)
        self.assertIn("a totally separate question", md)

    def test_default_path_and_confirmation(self):
        s = self._session()
        s.add_task("x").conversation = _conv_with_tool_call()
        app = _FakeApp(s)
        commands._export("", app)
        files = [f for f in os.listdir(self.dir) if f.startswith("2b-session-") and f.endswith(".md")]
        self.assertEqual(len(files), 1)
        self.assertTrue(any("Exported" in line and "messages" in line for line in app.ui.out))

    def test_empty_session_writes_nothing(self):
        app = _FakeApp(self._session())
        commands._export("", app)
        self.assertEqual(os.listdir(self.dir), [])
        self.assertTrue(any("empty" in line for line in app.ui.out))

    def test_task_error_is_included(self):
        # A turn that failed before any assistant message (provider 4xx/5xx, tool crash)
        # leaves only the user message in the conversation; the error lives on the task.
        s = self._session()
        c = Conversation(system_prompt="sys")
        c.append(Message.user("verify the xlsx data landed in menu_icon_name.dart"))
        t = s.add_task("verify")
        t.conversation = c
        t.error = "[openrouter] HTTP 404: No endpoints available matching your guardrail restrictions"
        app = _FakeApp(s)
        commands._export("out.md", app)
        md = open(os.path.join(self.dir, "out.md"), encoding="utf-8").read()
        self.assertIn("verify the xlsx data landed", md)
        self.assertIn("⚠ task error:", md)
        self.assertIn("HTTP 404", md)
        self.assertIn("1 error(s)", md)
        self.assertTrue(any("error(s)" in line for line in app.ui.out))

    def test_error_before_any_conversation_still_exports(self):
        # A model-resolve failure fails before a conversation is built; the error must
        # still be captured rather than "nothing to export".
        s = self._session()
        t = s.add_task("bad model")
        t.conversation = None
        t.error = "could not resolve model 'foo' to a configured provider"
        app = _FakeApp(s)
        commands._export("out.md", app)
        md = open(os.path.join(self.dir, "out.md"), encoding="utf-8").read()
        self.assertIn("## Errors", md)
        self.assertIn("could not resolve model", md)
        self.assertFalse(any("empty" in line for line in app.ui.out))

    def test_bad_path_reports_error_not_crash(self):
        s = self._session()
        s.add_task("x").conversation = _conv_with_tool_call()
        app = _FakeApp(s)
        commands._export("no_such_dir/out.md", app)     # parent dir doesn't exist
        self.assertTrue(any("failed" in line.lower() for line in app.ui.out))


if __name__ == "__main__":
    unittest.main()
