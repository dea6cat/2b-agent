"""`2b --test` also compares your installed models against the latest tool-capable coding
models on ollama.com (testcmd._suggest_coding). Pure host-side — discovery + machine RAM
are mocked, no network. Run: `python -m unittest tests.test_testcmd_coding`.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import discover, setup, testcmd  # noqa: E402


class SuggestCoding(unittest.TestCase):
    def _run(self, installed, found, ram=32):
        out = []
        with mock.patch.object(setup, "machine", return_value=(ram, False)), \
             mock.patch.object(discover, "discover", return_value=found):
            testcmd._suggest_coding(out.append, installed)
        return "\n".join(out)

    def test_shows_coding_models_not_installed(self):
        found = [("qwen2.5-coder:14b", 1_200_000, 16), ("deepseek-coder-v2:16b", 500_000, 18)]
        txt = self._run(installed=["qwen3:8b"], found=found)
        self.assertIn("qwen2.5-coder:14b", txt)
        self.assertIn("deepseek-coder-v2:16b", txt)
        self.assertIn("1.2M pulls", txt)                 # pull count formatted

    def test_hides_families_already_installed(self):
        found = [("qwen2.5-coder:14b", 1_200_000, 16), ("granite4:8b", 300_000, 10)]
        txt = self._run(installed=["qwen2.5-coder:7b"], found=found)   # same family, diff size
        self.assertNotIn("qwen2.5-coder", txt)           # family already installed → hidden
        self.assertIn("granite4:8b", txt)

    def test_note_when_all_popular_are_installed(self):
        found = [("qwen3:8b", 1_000_000, 10)]
        txt = self._run(installed=["qwen3:14b"], found=found)          # qwen3 family installed
        self.assertIn("already have", txt)

    def test_offline_note_when_ollama_unreachable(self):
        txt = self._run(installed=["qwen3:8b"], found=[])
        self.assertIn("Couldn't reach ollama.com", txt)


class FormatPulls(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(testcmd._fmt_pulls(1_200_000), "1.2M")
        self.assertEqual(testcmd._fmt_pulls(223_800), "224K")
        self.assertEqual(testcmd._fmt_pulls(742), "742")


if __name__ == "__main__":
    unittest.main()
