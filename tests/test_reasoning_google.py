"""Google maps a reasoning level to thinkingConfig.thinkingBudget (bounded, never dynamic).
Run: `python -m unittest tests.test_reasoning_google`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers.google import GoogleProvider, _G_LOW, _G_MED, _G_HIGH  # noqa: E402


class Budget(unittest.TestCase):
    def setUp(self):
        self.p = GoogleProvider()

    def test_capability_by_model(self):
        self.assertTrue(self.p.supports_reasoning("gemini-2.5-flash"))
        self.assertFalse(self.p.supports_reasoning("gemini-2.0-flash"))

    def test_none_is_capped_medium(self):
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", None), _G_MED)

    def test_levels(self):
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", "low"), _G_LOW)
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", "medium"), _G_MED)
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", "on"), _G_MED)
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", "high"), _G_HIGH)

    def test_off_disables_on_flash(self):
        self.assertEqual(self.p._thinking_budget("gemini-2.5-flash", "off"), 0)

    def test_off_uses_minimum_on_pro(self):
        self.assertEqual(self.p._thinking_budget("gemini-2.5-pro", "off"), 128)

    def test_unsupported_model_omits(self):
        self.assertIsNone(self.p._thinking_budget("gemini-2.0-flash", "high"))

    def test_payload_adds_thinkingconfig_only_when_budget_given(self):
        conv = type("C", (), {"system_prompt": "s", "messages": []})()
        with_b = self.p._payload(conv, (), thinking_budget=_G_MED)
        self.assertEqual(with_b["generationConfig"]["thinkingConfig"]["thinkingBudget"], _G_MED)
        self.assertNotIn("generationConfig", self.p._payload(conv, ()))


if __name__ == "__main__":
    unittest.main()
