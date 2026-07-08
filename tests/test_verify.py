"""Tests for deterministic verification (verify.py) + the done-verify nudge.

Placeholder/stub detection, repo-check discovery, and the once-per-task reminder
to verify edits. Host-side; the frozen schema is untouched.
Run: `python -m unittest tests.test_verify`.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator, verify  # noqa: E402
from two_b.conversation import Message, ToolCall  # noqa: E402
from two_b.orchestrator import EventType  # noqa: E402
from two_b.providers.base import ProviderResponse  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class ScanText(unittest.TestCase):
    def test_detects_not_implemented(self):
        self.assertIn("raises NotImplementedError", verify.scan_text("def f():\n    raise NotImplementedError"))
        self.assertTrue(verify.scan_text("void f() { throw UnimplementedError(); }"))

    def test_detects_your_code_here_and_implement_this(self):
        self.assertTrue(verify.scan_text("# your code here"))
        self.assertTrue(verify.scan_text("// your implementation goes here"))
        self.assertTrue(verify.scan_text("// implement this"))

    def test_clean_code_is_not_flagged(self):
        self.assertEqual(verify.scan_text("int add(int a, int b) => a + b;"), [])

    def test_no_false_positive_on_flutter_placeholder_or_implements(self):
        # 'placeholder' as an attribute and 'implements' in a class decl must not trip.
        self.assertEqual(verify.scan_text('TextField(placeholder: "Type here")'), [])
        self.assertEqual(verify.scan_text("class Foo implements Bar { int get x => 1; }"), [])
        self.assertEqual(verify.scan_text("// TODO: refactor later"), [])   # no 'implement'

    def test_weak_signals_deliberately_not_flagged(self):
        # Precision over recall: an aspirational TODO on complete code and an idiomatic
        # Python Protocol `...` body are NOT stub signals (too many false positives).
        self.assertEqual(verify.scan_text("def area(self) -> float: ...   # Protocol method"), [])
        self.assertEqual(verify.scan_text("x = 1  # TODO: implement caching for this later"), [])

    def test_empty_text(self):
        self.assertEqual(verify.scan_text(""), [])


class SummarizeEdit(unittest.TestCase):
    def test_note_when_stub_present(self):
        note = verify.summarize_edit("def f():\n    raise NotImplementedError\n")
        self.assertIn("placeholder/stub", note)

    def test_empty_when_clean(self):
        self.assertEqual(verify.summarize_edit("return 42"), "")

    def test_opt_out(self):
        os.environ["TWOB_NO_VERIFY"] = "1"
        try:
            self.assertEqual(verify.summarize_edit("raise NotImplementedError"), "")
        finally:
            del os.environ["TWOB_NO_VERIFY"]


class DiscoverChecks(unittest.TestCase):
    def _dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def test_package_json_scripts(self):
        d = self._dir()
        with open(os.path.join(d, "package.json"), "w") as f:
            f.write('{"scripts": {"test": "jest", "lint": "eslint .", "build": "tsc"}}')
        cmds = verify.discover_checks(d)
        self.assertIn("npm run test", cmds)
        self.assertIn("npm run lint", cmds)
        self.assertNotIn("npm run build", cmds)   # build isn't a check

    def test_pyproject_and_tests_dir(self):
        d = self._dir()
        with open(os.path.join(d, "pyproject.toml"), "w") as f:
            f.write("[tool.ruff]\nline-length = 100\n")
        os.mkdir(os.path.join(d, "tests"))
        cmds = verify.discover_checks(d)
        self.assertIn("pytest", cmds)
        self.assertIn("ruff check .", cmds)

    def test_pubspec(self):
        d = self._dir()
        open(os.path.join(d, "pubspec.yaml"), "w").close()
        os.mkdir(os.path.join(d, "test"))
        cmds = verify.discover_checks(d)
        self.assertIn("dart analyze", cmds)
        self.assertIn("dart test", cmds)

    def test_bare_dir_has_no_checks(self):
        self.assertEqual(verify.discover_checks(self._dir()), [])


class _EditThenFinalize:
    """Cloud-shaped provider: edits a file once, then keeps finalizing with plain text."""
    name, api_key = "fake", "x"

    def __init__(self):
        self.i, self.last_conv = 0, None

    def is_available(self):
        return True

    def list_models(self):
        return ["m"]

    def stream(self, conv, model, tools, on_text, *, cancel=None):
        self.last_conv = conv
        self.i += 1
        if self.i == 1:
            return ProviderResponse(message=Message.assistant(tool_calls=[
                ToolCall.new("edit_file", {"path": "x.dart", "old_text": "final a = 1;",
                                           "new_text": "final a = 2;"})]), raw={})
        on_text("done")
        return ProviderResponse(message=Message.assistant(text="done"), raw={})


class VerifyNudge(unittest.TestCase):
    def test_edits_without_verifying_get_one_nudge(self):
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        cwd = os.getcwd()
        os.chdir(proj)
        self.addCleanup(os.chdir, cwd)
        open(os.path.join(proj, "pubspec.yaml"), "w").close()     # so a repo check exists
        with open(os.path.join(proj, "x.dart"), "w") as f:
            f.write("final a = 1;\n")
        provider = _EditThenFinalize()
        s = Session(auto_yes=True, default_model="fake:m")          # auto-approve the edit
        t = Task(description="bump a")
        events = []
        orchestrator.run_task(s, t, events.append, {"fake": provider})
        self.assertIn(EventType.TASK_DONE, [e.type for e in events])
        # The model edited, then tried to finish; a single verify reminder was injected.
        nudges = [m for m in provider.last_conv.messages
                  if m.text and "haven't verified" in m.text]
        self.assertEqual(len(nudges), 1)

    def test_empty_finalizing_turn_is_not_nudged(self):
        # A finalizing turn with empty content must NOT be nudged — appending an empty
        # assistant message and re-sending would 400 on strict providers.
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        cwd = os.getcwd()
        os.chdir(proj)
        self.addCleanup(os.chdir, cwd)
        open(os.path.join(proj, "pubspec.yaml"), "w").close()
        with open(os.path.join(proj, "x.dart"), "w") as f:
            f.write("final a = 1;\n")

        class _EditThenEmpty(_EditThenFinalize):
            def stream(self, conv, model, tools, on_text, *, cancel=None):
                self.last_conv = conv
                self.i += 1
                if self.i == 1:
                    return ProviderResponse(message=Message.assistant(tool_calls=[
                        ToolCall.new("edit_file", {"path": "x.dart", "old_text": "final a = 1;",
                                                   "new_text": "final a = 2;"})]), raw={})
                return ProviderResponse(message=Message.assistant(), raw={})   # empty finalize

        provider = _EditThenEmpty()
        s = Session(auto_yes=True, default_model="fake:m")
        t = Task(description="bump a")
        events = []
        orchestrator.run_task(s, t, events.append, {"fake": provider})
        self.assertIn(EventType.TASK_DONE, [e.type for e in events])
        nudges = [m for m in provider.last_conv.messages if m.text and "haven't verified" in m.text]
        self.assertEqual(nudges, [])   # empty finalize was not nudged


if __name__ == "__main__":
    unittest.main()
