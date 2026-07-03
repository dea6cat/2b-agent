"""Tests for the eval harness scoring/parsing (no live model needed).

Covers the pure pieces — tool-call shape validity, trace parsing, the task
verifiers, and the §5 aggregation/check. `run_one`/`run_matrix` are integration
entry points (they drive the real `2b`), so they're exercised via `2b eval`, not
here. Run: `python -m unittest tests.test_evals` from the repo root.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import evals  # noqa: E402


class ShapeValidity(unittest.TestCase):
    def test_known_tool_with_required_args_is_valid(self):
        self.assertTrue(evals.shape_ok("read_file", {"path": "a.dart"}))
        self.assertTrue(evals.shape_ok("edit_file",
                                       {"path": "a", "old_text": "x", "new_text": "y"}))

    def test_missing_required_arg_is_invalid(self):
        self.assertFalse(evals.shape_ok("edit_file", {"path": "a", "old_text": "x"}))

    def test_optional_arg_may_be_omitted(self):
        # search_files' `path` is optional.
        self.assertTrue(evals.shape_ok("search_files", {"query": "foo"}))

    def test_unknown_tool_is_invalid(self):
        self.assertFalse(evals.shape_ok("teleport", {"x": 1}))

    def test_none_args_is_invalid_for_tool_needing_args(self):
        self.assertFalse(evals.shape_ok("read_file", None))

    def test_delegate_is_a_known_tool_for_cloud_models(self):
        # Cloud models get `delegate` on top of the frozen five; a well-formed
        # delegate call must count as valid, not unknown.
        from two_b.toolspec import DELEGATE_SPEC
        args = {p.name: "x" for p in DELEGATE_SPEC.params if p.required}
        self.assertTrue(evals.shape_ok(DELEGATE_SPEC.name, args))


class TraceParsing(unittest.TestCase):
    def _trace(self, events):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for e in events:
            f.write(json.dumps(e) + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_steps_and_valid_fraction(self):
        path = self._trace([
            {"t": "tool_call_start", "name": "read_file", "shown": {"path": "a"}},   # valid
            {"t": "tool_call_start", "name": "edit_file", "shown": {"path": "a", "old_text": "x"}},  # invalid
            {"t": "tool_call_start", "name": "bogus", "shown": {}},                   # invalid
            {"t": "tool_call_result", "name": "read_file", "result": "ok"},           # not a start
        ])
        steps, valid = evals.read_trace(path)
        self.assertEqual(steps, 3)
        self.assertAlmostEqual(valid, round(1 / 3, 3))

    def test_missing_trace_reads_as_no_calls_with_null_validity(self):
        # No calls -> validity is None (excluded from means), not a perfect 1.0.
        self.assertEqual(evals.read_trace("/no/such/trace.jsonl"), (0, None))

    def test_empty_and_malformed_lines_are_skipped(self):
        path = self._trace([{"t": "turn_start"}])   # a non-tool event
        with open(path, "a") as f:
            f.write("\n")            # blank
            f.write("not json\n")    # malformed
        steps, valid = evals.read_trace(path)
        self.assertEqual((steps, valid), (0, None))


class Verifiers(unittest.TestCase):
    def _dir(self, files):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for rel, content in files.items():
            with open(os.path.join(d, rel), "w") as f:
                f.write(content)
        return d

    def test_greeter_pass_and_fail(self):
        done = self._dir({"sample.dart":
            "class Greeter {\n"
            "  String greet(String name) => 'Hi there, $name!';\n"
            "  String farewell(String name) => 'Bye, $name!';\n"
            "}\n"})
        self.assertTrue(evals._greeter_ok(done))
        # Unedited fixture must fail.
        self.assertFalse(evals._greeter_ok(self._dir({"sample.dart": evals._GREETER})))

    def test_greeter_accepts_brace_interpolation(self):
        # `${name}` is valid Dart and equally correct — must not be rejected.
        braces = self._dir({"sample.dart":
            "class Greeter {\n"
            "  String greet(String name) => 'Hi there, ${name}!';\n"
            "  String farewell(String name) => 'Bye, ${name}!';\n"
            "}\n"})
        self.assertTrue(evals._greeter_ok(braces))

    def test_greeter_rejects_farewell_left_as_a_comment(self):
        # A dangling TODO comment must not pass as a real method.
        stub = self._dir({"sample.dart":
            "class Greeter {\n"
            "  String greet(String name) => 'Hi there, $name!';\n"
            "  // TODO: farewell -> 'Bye, $name!'\n"
            "}\n"})
        self.assertFalse(evals._greeter_ok(stub))

    def test_counter_pass_is_whitespace_tolerant(self):
        done = self._dir({"counter.dart":
            "class Counter {\n"
            "  int step = 1;\n"
            "  int value = 0;\n"
            "  void increment()=>value += step;\n"   # reflowed spacing
            "}\n"})
        self.assertTrue(evals._counter_ok(done))
        self.assertFalse(evals._counter_ok(self._dir({"counter.dart": evals._COUNTER})))

    def test_rename_requires_definition_and_call_site(self):
        done = self._dir({
            "math_utils.dart": "int sum(int a, int b) => a + b;\n",
            "calc.dart": "import 'math_utils.dart';\nint demo() => sum(2, 3);\n"})
        self.assertTrue(evals._rename_ok(done))
        # Renamed the definition but missed the call site -> fail.
        half = self._dir({
            "math_utils.dart": "int sum(int a, int b) => a + b;\n",
            "calc.dart": "import 'math_utils.dart';\nint demo() => add(2, 3);\n"})
        self.assertFalse(evals._rename_ok(half))


class Aggregation(unittest.TestCase):
    def _rows(self, model, condition, successes, steps):
        return [{"task_id": f"t{i}", "tier": "A", "model": model, "condition": condition,
                 "success": s, "landed": s, "analyze_clean": True,
                 "tool_call_valid": 1.0, "steps": st}
                for i, (s, st) in enumerate(zip(successes, steps))]

    def test_expected_metric_moved_is_detected(self):
        rows = (self._rows("m", "full", [True, True], [3, 3])
                + self._rows("m", "no_diagnostics", [False, True], [3, 3])   # success drops
                + self._rows("m", "no_semantics", [True, True], [7, 7]))     # steps rise
        _per, checks = evals.summarize(rows)
        by = {(c["condition"], c["metric"]): c for c in checks}
        self.assertTrue(by[("no_diagnostics", "success")]["moved"])
        self.assertTrue(by[("no_semantics", "steps")]["moved"])

    def test_flat_ablation_reads_as_not_moved(self):
        rows = (self._rows("m", "full", [True, True], [3, 3])
                + self._rows("m", "no_diagnostics", [True, True], [3, 3]))   # no change
        _per, checks = evals.summarize(rows)
        diag = next(c for c in checks if c["condition"] == "no_diagnostics")
        self.assertFalse(diag["moved"])

    def test_per_cell_means(self):
        per, _ = evals.summarize(self._rows("m", "full", [True, False], [2, 4]))
        cell = per[("m", "full")]
        self.assertEqual(cell["n"], 2)
        self.assertAlmostEqual(cell["success"], 0.5)
        self.assertAlmostEqual(cell["steps"], 3.0)

    def test_none_validity_is_excluded_from_the_mean(self):
        rows = [
            {"model": "m", "condition": "full", "success": True, "tool_call_valid": 1.0, "steps": 3},
            {"model": "m", "condition": "full", "success": False, "tool_call_valid": None, "steps": 0},
        ]
        per, _ = evals.summarize(rows)
        # Mean over the one non-None value, not dragged toward 0 or inflated to 1.
        self.assertAlmostEqual(per[("m", "full")]["tool_call_valid"], 1.0)

    def test_all_none_validity_cell_reports_none(self):
        rows = [{"model": "m", "condition": "full", "success": False,
                 "tool_call_valid": None, "steps": 0}]
        per, _ = evals.summarize(rows)
        self.assertIsNone(per[("m", "full")]["tool_call_valid"])


class Cli(unittest.TestCase):
    def _quiet(self, argv):
        import contextlib
        import io
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            return evals.main(argv)

    def test_list_returns_zero(self):
        self.assertEqual(self._quiet(["--list"]), 0)

    def test_missing_models_is_usage_error(self):
        self.assertEqual(self._quiet([]), 2)

    def test_task_set_is_frozen_and_tiered(self):
        tiers = {t.tier for t in evals.TASKS}
        self.assertEqual(tiers, {"A", "B", "C"})
        self.assertEqual(len({t.id for t in evals.TASKS}), len(evals.TASKS))  # unique ids


class TraceTapRoundTrip(unittest.TestCase):
    """The orchestrator's TWOB_TRACE tap must write what read_trace expects."""

    def test_tap_output_is_read_back(self):
        from two_b.orchestrator import AgentEvent, EventType, _traced
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.close()
        self.addCleanup(os.unlink, f.name)
        sink = _traced(lambda ev: None, f.name)   # real sink is a no-op here
        sink(AgentEvent(EventType.TOOL_CALL_START, "t1", {"name": "read_file", "shown": {"path": "a"}}))
        sink(AgentEvent(EventType.TOOL_CALL_START, "t1", {"name": "edit_file", "shown": {"path": "a"}}))
        sink(AgentEvent(EventType.TASK_DONE, "t1"))
        steps, valid = evals.read_trace(f.name)
        self.assertEqual(steps, 2)                 # two tool calls, TASK_DONE ignored
        self.assertAlmostEqual(valid, 0.5)         # read_file valid, edit_file missing old/new_text


if __name__ == "__main__":
    unittest.main()
