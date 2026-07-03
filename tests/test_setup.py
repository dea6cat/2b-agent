"""Tests for the pure logic of `2b setup` (grading, catalog, selection, verdict, grade
table). Shell/IO steps (Ollama install, pull, PATH) are exercised only for their decision
logic where practical. Run: `python -m unittest tests.test_setup` from the repo root.
"""
import os
import sys
import unittest

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

    def test_default_index_scales_with_ram(self):
        # low RAM → smallest (qwen3:4b, idx 0); high RAM → largest NON-opt-in (qwen3.5:9b, idx 2)
        self.assertEqual(setup.default_index(4), 0)
        self.assertEqual(setup.default_index(64), 2)               # never gemma4/coder (opt-in)
        self.assertFalse(setup.CATALOG[setup.default_index(64)].opt_in)


class Selection(unittest.TestCase):
    def test_empty_uses_default(self):
        self.assertEqual(setup.parse_selection("", 2), ["qwen3.5:9b"])
    def test_all(self):
        self.assertEqual(setup.parse_selection("all", 0), [m.name for m in setup.CATALOG])
    def test_numbers_and_commas(self):
        self.assertEqual(setup.parse_selection("1, 3", 0), ["qwen3:4b", "qwen3.5:9b"])
    def test_invalid_tokens_ignored(self):
        self.assertEqual(setup.parse_selection("2 nope 99 0", 0), ["qwen3:8b"])

    def test_default_model_prefers_qwen35(self):
        self.assertEqual(setup.default_model(["qwen3:8b", "qwen3.5:9b"], []), "qwen3.5:9b")
        self.assertEqual(setup.default_model(["qwen3:8b"], []), "qwen3:8b")
        self.assertEqual(setup.default_model([], ["llama3"]), "llama3")


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
