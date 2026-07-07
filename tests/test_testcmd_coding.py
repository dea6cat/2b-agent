"""`2b --test` compares installed models to the latest tool-capable coding models on
ollama.com (testcmd._coding_report), recommending the best-fitting family variant. In
`--test auto` it pulls + coding-tests the top candidate and recommends it only if it passes.
Pure host-side — discovery, RAM, pull, and grading are mocked (no network / no downloads).
Run: `python -m unittest tests.test_testcmd_coding`.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import config, discover, setup, testcmd  # noqa: E402


class Helpers(unittest.TestCase):
    def test_tag_family_size(self):
        self.assertEqual(testcmd._tag_family_size("qwen2.5-coder:14b"), ("qwen2.5-coder", 14.0))
        self.assertEqual(testcmd._tag_family_size("qwen3:8b-instruct-q4"), ("qwen3", 8.0))
        self.assertEqual(testcmd._tag_family_size("llama3.1:latest"), ("llama3.1", None))

    def test_family_sizes_keeps_largest_known(self):
        self.assertEqual(
            testcmd._family_sizes(["qwen3:8b", "qwen3:14b", "llama3.1:latest"]),
            {"qwen3": 14.0, "llama3.1": None})

    def test_fmt_pulls(self):
        self.assertEqual(testcmd._fmt_pulls(1_200_000), "1.2M")
        self.assertEqual(testcmd._fmt_pulls(223_800), "224K")
        self.assertEqual(testcmd._fmt_pulls(742), "742")


class CodingReport(unittest.TestCase):
    def _report(self, installed, found, ram=32, failed=None):
        out = []
        with mock.patch.object(setup, "machine", return_value=(ram, False)), \
             mock.patch.object(config, "get_prefs", return_value={"coding_failed": failed or []}), \
             mock.patch.object(discover, "discover", return_value=found):
            cands = testcmd._coding_report(out.append, installed)
        return "\n".join(out), cands

    def test_new_family_is_recommended(self):
        txt, cands = self._report(["qwen3:8b"], [("qwen2.5-coder:14b", 1_200_000, 16)])
        self.assertIn("qwen2.5-coder:14b", txt)
        self.assertEqual(cands, [("qwen2.5-coder:14b", 1_200_000, 16, None)])

    def test_bigger_variant_of_installed_family_is_an_upgrade(self):
        txt, cands = self._report(["qwen2.5-coder:7b"], [("qwen2.5-coder:14b", 900_000, 16)])
        self.assertIn("upgrade from :7b", txt)
        self.assertEqual(cands, [("qwen2.5-coder:14b", 900_000, 16, 7.0)])

    def test_same_or_smaller_variant_is_skipped(self):
        _txt, cands = self._report(["qwen2.5-coder:14b"], [("qwen2.5-coder:14b", 900_000, 16)])
        self.assertEqual(cands, [])          # already have the best-fitting variant

    def test_unknown_installed_size_is_left_alone(self):
        _txt, cands = self._report(["qwen2.5-coder:latest"], [("qwen2.5-coder:14b", 900_000, 16)])
        self.assertEqual(cands, [])          # can't compare → assume covered

    def test_offline_note_and_empty(self):
        txt, cands = self._report(["qwen3:8b"], [])
        self.assertEqual(cands, [])
        self.assertIn("Couldn't reach ollama.com", txt)

    def test_previously_failed_candidate_is_skipped(self):
        # a candidate auto already pulled + failed here is remembered and not re-suggested
        _txt, cands = self._report(["qwen3:8b"], [("gemma4:12b", 100, 14)], failed=["gemma4:12b"])
        self.assertEqual(cands, [])


class AutoPullTest(unittest.TestCase):
    def _run_auto(self, candidate_passes=True, download=True, assume_yes=False, confirm="record"):
        pulled, removed, prefs, prompts = [], [], [], []

        def fake_ct(m):
            return ((candidate_passes, 1) if m == "qwen2.5-coder:14b" else (True, 1))

        def _recording_confirm(p):
            prompts.append(p)
            return download
        conf = _recording_confirm if confirm == "record" else confirm

        with mock.patch.object(setup, "installed_models", return_value=["qwen3:8b"]), \
             mock.patch.object(setup, "ensure_server", return_value=True), \
             mock.patch.object(setup, "_toks", return_value=50.0), \
             mock.patch.object(setup, "_ps_mem_gpu", return_value=("", "")), \
             mock.patch.object(setup, "correctness_test", side_effect=fake_ct), \
             mock.patch.object(setup, "grade_table", return_value=(["(grade)"], "qwen3:8b")), \
             mock.patch.object(setup, "machine", return_value=(32, False)), \
             mock.patch.object(setup, "_gb_est", return_value=1.0), \
             mock.patch.object(setup, "pull", side_effect=lambda models, emit: pulled.extend(models)), \
             mock.patch.object(setup, "remove_models", side_effect=lambda models, emit: removed.extend(models)), \
             mock.patch.object(config, "get_prefs", return_value={}), \
             mock.patch.object(config, "set_pref", side_effect=lambda k, v: prefs.append((k, v))), \
             mock.patch.object(discover, "discover", return_value=[("qwen2.5-coder:14b", 1_200_000, 16)]):
            out = []
            code = testcmd.run(out.append, auto=True, confirm=conf, assume_yes=assume_yes)
        return {"txt": "\n".join(out), "pulled": pulled, "removed": removed,
                "prefs": prefs, "prompts": prompts, "code": code}

    def test_asks_before_downloading_but_not_before_removing(self):
        r = self._run_auto(candidate_passes=True, download=True)
        self.assertTrue(any("Pull" in p for p in r["prompts"]))     # asked before the download
        self.assertFalse(any("Remove" in p for p in r["prompts"]))  # removal is silent
        self.assertEqual(r["pulled"], ["qwen2.5-coder:14b"])
        self.assertIn("Coding-testing qwen2.5-coder:14b", r["txt"])   # progress shown before the test
        self.assertIn("passed the coding test", r["txt"])

    def test_declining_the_download_skips_the_pull(self):
        r = self._run_auto(candidate_passes=True, download=False)
        self.assertTrue(any("Pull" in p for p in r["prompts"]))
        self.assertEqual(r["pulled"], [])                            # declined → no download
        self.assertIn("Skipped", r["txt"])

    def test_yes_skips_the_download_prompt(self):
        def _must_not_ask(_p):
            raise AssertionError("--yes should not prompt")
        r = self._run_auto(candidate_passes=True, assume_yes=True, confirm=_must_not_ask)
        self.assertEqual(r["pulled"], ["qwen2.5-coder:14b"])         # pulled without asking

    def test_failing_candidate_is_removed_and_remembered(self):
        r = self._run_auto(candidate_passes=False, download=True)
        self.assertIn("qwen2.5-coder:14b", r["removed"])            # pulled dud removed
        self.assertEqual(r["prefs"], [("coding_failed", ["qwen2.5-coder:14b"])])   # ...and remembered


if __name__ == "__main__":
    unittest.main()
