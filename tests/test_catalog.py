"""Tests for the per-model capability catalog (Phase 6.1). Covers prefix matching,
the accessor fallbacks, corrupt-catalog degradation, and the context_budget wiring
(catalog window for known cloud models, per-provider constant otherwise).
Run: `python -m unittest tests.test_catalog`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import catalog  # noqa: E402
from two_b import orchestrator  # noqa: E402


class _Prov:
    def __init__(self, name):
        self.name = name


class CatalogLookup(unittest.TestCase):
    def test_exact_key_matches(self):
        info = catalog.lookup("gpt-4o-mini")
        self.assertIsNotNone(info)
        self.assertEqual(info.context_window, 128000)

    def test_dated_variant_matches_base_prefix(self):
        # gpt-4o-2024-08-06 has no exact key; falls to the 'gpt-4o' prefix.
        info = catalog.lookup("gpt-4o-2024-08-06")
        self.assertIsNotNone(info)
        self.assertEqual(info.context_window, 128000)
        self.assertEqual(info.default_max_tokens, 16384)

    def test_longest_prefix_wins(self):
        # 'gpt-4o-mini-...' must resolve to gpt-4o-mini, not the shorter gpt-4o.
        mini = catalog.lookup("gpt-4o-mini-2024-07-18")
        base = catalog.lookup("gpt-4o-2024-08-06")
        self.assertEqual(mini.default_max_tokens, 16384)
        self.assertEqual(base.default_max_tokens, 16384)
        # 'o1-mini' (128k) is a longer prefix than the bare 'o1' full model (200k),
        # so a reasoning-mini variant must not inherit the larger window.
        self.assertEqual(catalog.lookup("o1-mini-2024-09-12").context_window, 128000)
        self.assertEqual(catalog.lookup("o1-2024-12-17").context_window, 200000)

    def test_case_insensitive(self):
        self.assertEqual(catalog.lookup("GPT-4O"), catalog.lookup("gpt-4o"))

    def test_unknown_model_is_none(self):
        self.assertIsNone(catalog.lookup("some-unknown-model-xyz"))
        self.assertIsNone(catalog.lookup(""))


class CatalogAccessors(unittest.TestCase):
    def test_context_window_fallback(self):
        self.assertEqual(catalog.context_window("gpt-4o"), 128000)
        self.assertIsNone(catalog.context_window("nope-nope"))

    def test_max_tokens_uses_default_when_unknown(self):
        self.assertEqual(catalog.max_tokens("claude-opus-4-8", 4096), 8192)
        self.assertEqual(catalog.max_tokens("nope-nope", 4096), 4096)

    def test_supports_images(self):
        self.assertTrue(catalog.supports_images("gpt-4o"))
        self.assertFalse(catalog.supports_images("gpt-3.5-turbo"))
        self.assertFalse(catalog.supports_images("nope-nope"))


class CorruptCatalog(unittest.TestCase):
    def test_bad_json_degrades_to_empty(self):
        real = catalog._CATALOG_PATH
        real_cache = catalog._cache
        try:
            catalog._CATALOG_PATH = real.with_name("does-not-exist.json")
            catalog._cache = None
            self.assertIsNone(catalog.lookup("gpt-4o"))
            self.assertEqual(catalog.max_tokens("gpt-4o", 4096), 4096)
        finally:
            catalog._CATALOG_PATH = real
            catalog._cache = real_cache


class ContextBudgetWiring(unittest.TestCase):
    def test_cloud_known_model_uses_catalog_window(self):
        self.assertEqual(orchestrator.context_budget(_Prov("anthropic"), "claude-opus-4-8"), 200000)
        self.assertEqual(orchestrator.context_budget(_Prov("openai"), "gpt-4o"), 128000)

    def test_cloud_unknown_model_uses_provider_constant(self):
        # openai's per-provider fallback is 120000; an unlisted model hits it.
        self.assertEqual(orchestrator.context_budget(_Prov("openai"), "mystery-model"), 120000)

    def test_unknown_provider_and_model_floor(self):
        self.assertEqual(orchestrator.context_budget(_Prov("whoknows"), "mystery"), 8000)

    def test_local_ollama_uses_provider_window(self):
        class _Ollama:
            name = "ollama"
            def context_window(self, model):
                return 4321
        self.assertEqual(orchestrator.context_budget(_Ollama(), "qwen3.5:9b"), 4321)

    def test_ollama_cloud_uses_provider_window_not_catalog(self):
        class _OllamaCloud:
            name = "ollama-cloud"
            def context_window(self, model):
                return 120000
        self.assertEqual(orchestrator.context_budget(_OllamaCloud(), "gpt-oss:120b"), 120000)


if __name__ == "__main__":
    unittest.main()
