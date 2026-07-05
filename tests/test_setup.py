"""Tests for the pure logic of `2b setup` (grading, catalog, selection, verdict, grade
table). Shell/IO steps (Ollama install, pull, PATH) are exercised only for their decision
logic where practical. Run: `python -m unittest tests.test_setup` from the repo root.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import setup  # noqa: E402


class Grade(unittest.TestCase):
    def test_machine_shape(self):
        ram, apple = setup.machine()
        self.assertIsInstance(ram, int)
        self.assertIsInstance(apple, bool)

    def test_fit_tag(self):
        self.assertEqual(setup.fit_tag(11, 16), "✓ fits well")
        self.assertEqual(setup.fit_tag(11, 9), "~ tight")          # within 3GB below
        self.assertEqual(setup.fit_tag(16, 8), "✗ needs 16GB+")

    def test_default_index_picks_recommended_else_first(self):
        # candidates are (tag, est_ram, label), already popularity-ranked
        cands = [("a:8b", 10, ""), ("b:9b", 11, ""), ("c:4b", 6, "")]
        self.assertEqual(setup.default_index(cands, "b:9b"), 1)     # recommended present
        self.assertEqual(setup.default_index(cands, "nope"), 0)     # else the top-ranked
        self.assertEqual(setup.default_index([], "x"), 0)

    def test_bundled_catalog_parses(self):
        cat, rec = setup.bundled_catalog()
        self.assertTrue(cat and all(m.name and m.min_ram_gb for m in cat))
        self.assertIn(rec, [m.name for m in cat])                  # recommended is a listed model


class Candidates(unittest.TestCase):
    def test_gb_estimate(self):
        self.assertEqual(setup._gb_est("qwen3:8b"), 5.6)
        self.assertEqual(setup._gb_est("weird-tag-no-size"), 0.0)

    def test_uses_discovery_when_available(self):
        rows = [("a:8b", 16_900_000, 10), ("b:4b", 5_000, 6)]
        with mock.patch.object(setup.discover, "discover", return_value=rows):
            cands, rec, source = setup._candidates(64, {})
        self.assertEqual(source, "web")
        self.assertEqual([c[0] for c in cands], ["a:8b", "b:4b"])
        self.assertEqual(rec, "a:8b")                          # top-ranked is the default
        self.assertIn("16.9M pulls", cands[0][2])

    def test_falls_back_to_bundled(self):
        with mock.patch.object(setup.discover, "discover", return_value=[]):
            cands, rec, source = setup._candidates(64, {})
        self.assertEqual(source, "bundled")
        self.assertIn(rec, [c[0] for c in cands])

    def test_no_discover_forces_bundled(self):
        with mock.patch.object(setup.discover, "discover", return_value=[("x:8b", 999, 10)]):
            _, _, source = setup._candidates(64, {"no_discover": True})
        self.assertEqual(source, "bundled")

    def test_bundled_recommended_never_opt_in(self):
        import json
        js = json.dumps({"recommended": "big:12b", "models": [
            {"name": "small:4b", "min_ram_gb": 6, "opt_in": False},
            {"name": "big:12b", "min_ram_gb": 14, "opt_in": True}]})
        with mock.patch("pathlib.Path.read_text", return_value=js):
            _cat, rec = setup.bundled_catalog()
        self.assertEqual(rec, "small:4b")                  # opt-in default is rejected


class Prune(unittest.TestCase):
    def test_keeps_chosen_and_selected_removes_rest(self):
        pulled = {"a:8b", "b:9b", "c:4b"}
        # user selected b, chose b as default → a and c are the losers
        self.assertEqual(setup.prunable_models(pulled, "b:9b", ["b:9b"]), ["a:8b", "c:4b"])

    def test_multi_select_keeps_all_selected(self):
        pulled = {"a:8b", "b:9b", "c:4b"}
        self.assertEqual(setup.prunable_models(pulled, "a:8b", ["a:8b", "c:4b"]), ["b:9b"])

    def test_nothing_pretested_is_noop(self):
        self.assertEqual(setup.prunable_models(set(), "a:8b", ["a:8b"]), [])

    def test_chosen_not_in_pretested_still_prunes_pretested(self):
        # chosen came from an already-installed model; pretested losers still cleaned up
        self.assertEqual(setup.prunable_models({"a:8b", "b:9b"}, "old:7b", []), ["a:8b", "b:9b"])

    def test_remove_models_reports_failure_on_nonzero_exit(self):
        import subprocess
        msgs = []
        ok = subprocess.CompletedProcess([], 0, "", "")
        fail = subprocess.CompletedProcess([], 1, "", "Error: model not found")
        with mock.patch("subprocess.run", side_effect=[ok, fail]):
            setup.remove_models(["a:8b", "b:9b"], msgs.append)
        self.assertIn("removed a:8b", msgs)
        self.assertTrue(any("could not remove b:9b" in m and "model not found" in m for m in msgs))


class Selection(unittest.TestCase):
    TAGS = ["qwen3:4b", "qwen3:8b", "qwen3.5:9b"]

    def test_empty_uses_default(self):
        self.assertEqual(setup.parse_selection("", 2, self.TAGS), ["qwen3.5:9b"])
    def test_all(self):
        self.assertEqual(setup.parse_selection("all", 0, self.TAGS), self.TAGS)
    def test_numbers_and_commas(self):
        self.assertEqual(setup.parse_selection("1, 3", 0, self.TAGS), ["qwen3:4b", "qwen3.5:9b"])
    def test_invalid_tokens_ignored(self):
        self.assertEqual(setup.parse_selection("2 nope 99 0", 0, self.TAGS), ["qwen3:8b"])

    def test_default_model_prefers_recommended(self):
        self.assertEqual(setup.default_model(["qwen3:8b", "qwen3.5:9b"], [], "qwen3.5:9b"), "qwen3.5:9b")
        self.assertEqual(setup.default_model(["qwen3:8b"], [], "qwen3.5:9b"), "qwen3:8b")
        self.assertEqual(setup.default_model([], ["llama3"], "qwen3.5:9b"), "llama3")


class Verdict(unittest.TestCase):
    def test_correct(self):
        self.assertTrue(setup.verdict("class Greeter { String greet(String n) => 'Hi there, $name!';"
                                      " String farewell(String n) => 'Bye, $name!'; }"))
    def test_partial_fails(self):
        self.assertFalse(setup.verdict("greet => 'Hi there, $name!';"))          # no farewell
        self.assertFalse(setup.verdict("greet => 'Hello, $name!';"))             # old greeting remains


class GradeTable(unittest.TestCase):
    def test_keep_remove_and_best(self):
        perf = {"a": (22.2, "5.9 GB", "yes"), "b": (24.1, "7.7 GB", "yes")}
        correctness = {"a": (True, 14), "b": (False, 150)}
        rows, best = setup.grade_table(perf, correctness)
        joined = "\n".join(rows)
        self.assertIn("KEEP", joined)
        self.assertIn("REMOVE", joined)
        self.assertEqual(best, "a")                                # fastest passing (b is REMOVE)
    def test_missing_perf_renders_placeholder(self):
        rows, best = setup.grade_table({}, {"a": (True, 20)})
        self.assertIn("?", "\n".join(rows))
        self.assertEqual(best, "a")


class PathFix(unittest.TestCase):
    def setUp(self):
        self._orig_bin = setup._bin_dir
        setup._bin_dir = lambda: "/opt/uvbin"
        self._path = os.environ.get("PATH")
        self._orig = os.environ.get("_2B_ORIG_PATH")

    def tearDown(self):
        setup._bin_dir = self._orig_bin
        if self._path is not None:
            os.environ["PATH"] = self._path
        os.environ.pop("_2B_ORIG_PATH", None)
        if self._orig is not None:
            os.environ["_2B_ORIG_PATH"] = self._orig

    def test_bindir_absent_needs_fix(self):
        os.environ["PATH"] = "/usr/bin"; os.environ.pop("_2B_ORIG_PATH", None)
        self.assertTrue(setup._path_needs_fix())

    def test_installer_prepend_still_detects_missing_persistent(self):
        # live PATH has bindir (installer prepended it) but the ORIG/persistent PATH doesn't
        os.environ["PATH"] = "/opt/uvbin:/usr/bin"
        os.environ["_2B_ORIG_PATH"] = "/usr/bin"
        self.assertTrue(setup._path_needs_fix())   # the I1 case

    def test_persistent_has_bindir_no_fix(self):
        os.environ["_2B_ORIG_PATH"] = "/opt/uvbin:/usr/bin"
        self.assertFalse(setup._path_needs_fix())


class NonInteractive(unittest.TestCase):
    def test_ask_confirm_use_defaults_under_yes(self):
        opts = {"yes": True}
        self.assertEqual(setup._ask("x", "def", opts), "def")
        self.assertTrue(setup._confirm("x", True, opts))
        self.assertFalse(setup._confirm("x", False, opts))


if __name__ == "__main__":
    unittest.main()
