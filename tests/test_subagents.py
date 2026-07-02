import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from two_b import subagents

class ReadDispatch(unittest.TestCase):
    def test_write_tools_refused(self):
        self.assertIn("not available", subagents._read_dispatch("edit_file", {"path":"x"}, None))
        self.assertIn("not available", subagents._read_dispatch("run_command", {"command":"ls"}, None))
    def test_read_tools_allowed(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.py"), "w") as f:
            f.write("x=1\n")
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

    def test_batch_failure_isolation(self):
        def flaky(goal, *a, **k):
            if goal == "bad":
                raise RuntimeError("boom")
            return f"found: {goal}"
        subagents.run_explorer = flaky
        out = subagents.delegate(
            [{"role": "explore", "goal": "bad"}, {"role": "explore", "goal": "good"}],
            provider=None, model="m")
        self.assertIn("### [1] explore: bad", out)
        self.assertIn("explorer error", out)
        self.assertIn("### [2] explore: good", out)
        self.assertIn("found: good", out)

    def test_batch_timeout_does_not_touch_parent_cancel(self):
        import threading, time
        from two_b import subagents
        parent = threading.Event()
        subagents.run_explorer = lambda goal, *a, **k: (time.sleep(0.5) or "late")  # slower than the tiny budget
        orig = subagents.DELEGATE_TIMEOUT
        subagents.DELEGATE_TIMEOUT = 0.05
        try:
            out = subagents.delegate([{"role":"explore","goal":"slow"}], provider=None, model="m", cancel=parent)
        finally:
            subagents.DELEGATE_TIMEOUT = orig
        self.assertFalse(parent.is_set())          # parent task must NOT be cancelled
        self.assertIn("(timed out)", out)


class WorkerFS(unittest.TestCase):
    def _file(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False); f.write(text); f.close(); return f.name
    def test_edit_then_edit_stacks(self):
        p = self._file("a = 1\nb = 2\n"); fs = subagents._WorkerFS()
        self.assertIn("recorded", fs.edit(p, "a = 1", "a = 9"))
        self.assertIn("recorded", fs.edit(p, "b = 2", "b = 8"))   # second edit sees the first
        (path, orig, final), = fs.changes()
        self.assertEqual(orig, "a = 1\nb = 2\n"); self.assertEqual(final, "a = 9\nb = 8\n")
    def test_read_sees_pending(self):
        p = self._file("x = 1\n"); fs = subagents._WorkerFS(); fs.edit(p, "x = 1", "x = 2")
        self.assertIn("x = 2", fs.read(p))
    def test_failed_edit_records_nothing(self):
        p = self._file("x = 1\n"); fs = subagents._WorkerFS()
        self.assertIn("error", fs.edit(p, "nope", "y"))
        self.assertEqual(fs.changes(), [])
    def test_write_then_change_detected(self):
        p = self._file("old\n"); fs = subagents._WorkerFS(); fs.write(p, "new\n")
        (path, orig, final), = fs.changes(); self.assertEqual((orig, final), ("old\n", "new\n"))
    def test_noop_write_not_a_change(self):
        p = self._file("same\n"); fs = subagents._WorkerFS(); fs.write(p, "same\n")
        self.assertEqual(fs.changes(), [])
