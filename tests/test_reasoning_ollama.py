"""Ollama maps a reasoning level to the `think` field, gated on model capability.
Run: `python -m unittest tests.test_reasoning_ollama`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers.ollama import OllamaProvider  # noqa: E402


def _prov(caps):
    p = OllamaProvider()
    p._show = lambda model: {"capabilities": caps}     # bypass network
    return p


class ThinkValue(unittest.TestCase):
    def test_capable_model_reports_supported(self):
        self.assertTrue(_prov(["completion", "tools", "thinking"]).supports_reasoning("qwen3.5:9b"))
        self.assertFalse(_prov(["completion", "tools"]).supports_reasoning("llama3:8b"))

    def test_none_reasoning_omits_think(self):
        self.assertIsNone(_prov(["thinking"])._think_value("qwen3.5:9b", None))

    def test_off_on(self):
        p = _prov(["thinking"])
        self.assertIs(p._think_value("qwen3.5:9b", "off"), False)
        self.assertIs(p._think_value("qwen3.5:9b", "on"), True)

    def test_level_is_boolean_true_for_ordinary_model(self):
        self.assertIs(_prov(["thinking"])._think_value("qwen3.5:9b", "high"), True)

    def test_level_is_native_string_for_gpt_oss(self):
        self.assertEqual(_prov(["thinking"])._think_value("gpt-oss:20b", "high"), "high")

    def test_noncapable_model_never_gets_think(self):
        p = _prov(["completion", "tools"])
        for lvl in ("off", "on", "low", "medium", "high", None):
            self.assertIsNone(p._think_value("llama3:8b", lvl))


if __name__ == "__main__":
    unittest.main()
