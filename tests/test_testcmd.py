"""Tests for `2b --test` (src/two_b/testcmd.py) — grading installed models and the auto
cleanup of failures. Setup's grading helpers are stubbed; no Ollama/2b subprocess runs.
Run: `python -m unittest tests.test_testcmd`.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import testcmd  # noqa: E402


def _msgs():
    out = []
    return out, out.append


class DefaultTag(unittest.TestCase):
    def test_strips_provider_prefix(self):
        self.assertEqual(testcmd._default_tag({"default_model": "ollama:qwen3:8b"}), "qwen3:8b")
    def test_empty_when_unset(self):
        self.assertEqual(testcmd._default_tag({}), "")


class Preconditions(unittest.TestCase):
    def test_no_models_installed(self):
        out, emit = _msgs()
        with mock.patch.object(testcmd.setup, "installed_models", return_value=[]):
            rc = testcmd.run(emit)
        self.assertEqual(rc, 1)
        self.assertTrue(any("No local models" in m for m in out))

    def test_target_not_installed(self):
        out, emit = _msgs()
        with mock.patch.object(testcmd.setup, "installed_models", return_value=["a:8b"]):
            rc = testcmd.run(emit, target="ghost:9b")
        self.assertEqual(rc, 1)
        self.assertTrue(any("isn't installed" in m for m in out))

    def test_server_down(self):
        out, emit = _msgs()
        with mock.patch.object(testcmd.setup, "installed_models", return_value=["a:8b"]), \
             mock.patch.object(testcmd.setup, "ensure_server", return_value=False):
            rc = testcmd.run(emit)
        self.assertEqual(rc, 1)
        self.assertTrue(any("isn't reachable" in m for m in out))

    def test_2b_not_on_path(self):
        out, emit = _msgs()
        with mock.patch.object(testcmd.setup, "installed_models", return_value=["a:8b"]), \
             mock.patch.object(testcmd.setup, "ensure_server", return_value=True), \
             mock.patch.object(testcmd.setup, "_toks", return_value=10.0), \
             mock.patch.object(testcmd.setup, "_ps_mem_gpu", return_value=("5 GB", "yes")), \
             mock.patch.object(testcmd.setup, "correctness_test", return_value=None):
            rc = testcmd.run(emit)
        self.assertEqual(rc, 1)
        self.assertTrue(any("isn't on your PATH" in m for m in out))


class Grading(unittest.TestCase):
    def _patches(self, correctness):
        return [
            mock.patch.object(testcmd.setup, "installed_models", return_value=list(correctness)),
            mock.patch.object(testcmd.setup, "ensure_server", return_value=True),
            mock.patch.object(testcmd.setup, "_toks", side_effect=lambda m: 20.0),
            mock.patch.object(testcmd.setup, "_ps_mem_gpu", return_value=("5 GB", "yes")),
            mock.patch.object(testcmd.setup, "correctness_test", side_effect=lambda m: correctness[m]),
            mock.patch.object(testcmd.discover, "discover", return_value=[]),   # no network / coding pull
        ]

    def test_grades_all_and_suggests_best(self):
        out, emit = _msgs()
        cs = {"a:8b": (True, 12), "b:9b": (False, 90)}
        with mock.patch.object(testcmd.config, "get_prefs", return_value={}):
            for p in self._patches(cs):
                p.start()
            try:
                rc = testcmd.run(emit)
            finally:
                mock.patch.stopall()
        self.assertEqual(rc, 0)
        joined = "\n".join(out)
        self.assertIn("KEEP", joined)
        self.assertIn("REMOVE", joined)
        self.assertIn("suggested default", joined)


class Auto(unittest.TestCase):
    def _run_auto(self, correctness, prefs, confirm=None, assume_yes=False):
        out, emit = _msgs()
        removed = []
        with mock.patch.object(testcmd.config, "get_prefs", return_value=prefs), \
             mock.patch.object(testcmd.setup, "installed_models", return_value=list(correctness)), \
             mock.patch.object(testcmd.setup, "ensure_server", return_value=True), \
             mock.patch.object(testcmd.setup, "_toks", side_effect=lambda m: 20.0), \
             mock.patch.object(testcmd.setup, "_ps_mem_gpu", return_value=("5 GB", "yes")), \
             mock.patch.object(testcmd.setup, "_gb_est", side_effect=lambda m: 5.0), \
             mock.patch.object(testcmd.setup, "correctness_test", side_effect=lambda m: correctness[m]), \
             mock.patch.object(testcmd.setup, "remove_models", side_effect=lambda ms, e: removed.extend(ms)), \
             mock.patch.object(testcmd.discover, "discover", return_value=[]):   # no network / coding pull
            rc = testcmd.run(emit, auto=True, confirm=confirm, assume_yes=assume_yes)
        return rc, out, removed

    def test_removes_failed_when_confirmed(self):
        cs = {"good:8b": (True, 12), "bad:9b": (False, 90)}
        rc, out, removed = self._run_auto(cs, {}, confirm=lambda p: True)
        self.assertEqual(rc, 0)
        self.assertEqual(removed, ["bad:9b"])

    def test_auto_never_prompts(self):
        # auto is fully automatic — it must remove failures without ever calling confirm.
        cs = {"good:8b": (True, 12), "bad:9b": (False, 90)}

        def _must_not_ask(_p):
            raise AssertionError("--test auto must not prompt")
        rc, out, removed = self._run_auto(cs, {}, confirm=_must_not_ask)
        self.assertEqual(removed, ["bad:9b"])

    def test_protects_default_even_if_it_failed(self):
        cs = {"bad:9b": (False, 90)}
        rc, out, removed = self._run_auto(cs, {"default_model": "ollama:bad:9b"},
                                          confirm=lambda p: True)
        self.assertEqual(removed, [])                      # the failing model IS the default
        self.assertTrue(any("current default" in m for m in out))

    def test_assume_yes_removes_without_confirm(self):
        cs = {"good:8b": (True, 12), "bad:9b": (False, 90)}
        rc, out, removed = self._run_auto(cs, {}, confirm=None, assume_yes=True)
        self.assertEqual(removed, ["bad:9b"])

    def test_no_failures_removes_nothing(self):
        cs = {"good:8b": (True, 12)}
        rc, out, removed = self._run_auto(cs, {}, confirm=lambda p: True)
        self.assertEqual(removed, [])
        self.assertTrue(any("No failing models to remove" in m for m in out))


if __name__ == "__main__":
    unittest.main()
