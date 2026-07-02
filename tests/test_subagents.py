import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from two_b import subagents

class ReadDispatch(unittest.TestCase):
    def test_write_tools_refused(self):
        self.assertIn("not available", subagents._read_dispatch("edit_file", {"path":"x"}, None))
        self.assertIn("not available", subagents._read_dispatch("run_command", {"command":"ls"}, None))
    def test_read_tools_allowed(self):
        d = tempfile.mkdtemp(); open(os.path.join(d,"a.py"),"w").write("x=1\n")
        out = subagents._read_dispatch("list_files", {"path": d}, None)
        self.assertIn("a.py", out)


class RunExplorer(unittest.TestCase):
    def test_loops_then_returns_final_text(self):
        from two_b.conversation import Message, ToolCall
        calls = iter([
            Message.assistant(tool_calls=[ToolCall.new("search_files", {"query":"Widget"})]),
            Message.assistant(text="Widget is defined in a.py:1"),
        ])
        class FakeProvider:
            name = "anthropic"
            def stream(self, conv, model, tools_, on_text):
                from two_b.providers.base import ProviderResponse
                return ProviderResponse(message=next(calls), raw={})
        out = subagents.run_explorer("find Widget", FakeProvider(), "m")
        self.assertEqual(out, "Widget is defined in a.py:1")


class Delegate(unittest.TestCase):
    def setUp(self):
        self._orig_run_explorer = subagents.run_explorer

    def tearDown(self):
        subagents.run_explorer = self._orig_run_explorer

    def test_digest_has_one_section_per_task(self):
        subagents.run_explorer = lambda goal, *a, **k: f"found: {goal}"   # stub
        out = subagents.delegate(
            [{"role":"explore","goal":"A"}, {"role":"explore","goal":"B"}],
            provider=None, model="m")
        self.assertIn("A", out); self.assertIn("B", out)
        self.assertIn("found: A", out); self.assertIn("found: B", out)

    def test_work_role_stubbed(self):
        out = subagents.delegate([{"role":"work","goal":"edit x"}], provider=None, model="m")
        self.assertIn("not enabled yet", out)
