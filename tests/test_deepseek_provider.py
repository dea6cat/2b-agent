"""DeepSeek is an OpenAI-compatible provider — registered as data in _OPENAI_COMPAT plus
its key-env mapping. These lock in that wiring. Pure host-side.
Run: `python -m unittest tests.test_deepseek_provider`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import config, registry  # noqa: E402


class DeepSeekProvider(unittest.TestCase):
    def test_registered_as_openai_compatible_cloud(self):
        p = registry.build_registry()["deepseek"]
        self.assertEqual(p.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(p.key_env, "DEEPSEEK_API_KEY")
        self.assertFalse(registry.is_local(p))          # cloud, not local Ollama

    def test_key_env_mapping(self):
        self.assertEqual(config.PROVIDER_KEY_ENV["deepseek"], "DEEPSEEK_API_KEY")

    def test_available_and_resolvable_with_key(self):
        old = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "sk-test-key"
        try:
            reg = registry.build_registry()
            self.assertTrue(reg["deepseek"].is_available())
            resolved = registry.resolve(reg, "deepseek:deepseek-chat")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved[0].name, "deepseek")
            self.assertEqual(resolved[1], "deepseek-chat")
        finally:
            if old is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = old

    def test_unavailable_without_key(self):
        old = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            self.assertFalse(registry.build_registry()["deepseek"].is_available())
        finally:
            if old is not None:
                os.environ["DEEPSEEK_API_KEY"] = old


if __name__ == "__main__":
    unittest.main()
