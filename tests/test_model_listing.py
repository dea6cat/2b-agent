"""OpenAICompatProvider.list_models pulls the live list from /models with the key. On
failure it returns [] — never a fabricated fallback that would hide "the API is down / I
can't connect" — and doesn't cache the failure, so it retries. Pure host-side.
Run: `python -m unittest tests.test_model_listing`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers import openai_compat as oc  # noqa: E402
from two_b.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from two_b.providers import anthropic as an, google as g  # noqa: E402
from two_b.providers.anthropic import AnthropicProvider  # noqa: E402
from two_b.providers.google import GoogleProvider  # noqa: E402


class ModelListing(unittest.TestCase):
    def setUp(self):
        self._orig = oc.get_json
        os.environ["FAKE_KEY"] = "k"

    def tearDown(self):
        oc.get_json = self._orig
        os.environ.pop("FAKE_KEY", None)

    def _dynamic(self):
        return OpenAICompatProvider("fake", "https://x/v1", "FAKE_KEY", models=[], dynamic_models=True)

    def test_lists_live_models_from_the_api(self):
        oc.get_json = lambda *a, **k: {"data": [{"id": "zai-glm-4.7"}, {"id": "gpt-oss-120b"}]}
        self.assertEqual(self._dynamic().list_models(), ["gpt-oss-120b", "zai-glm-4.7"])  # sorted live list

    def test_failure_returns_empty_not_a_fabricated_list(self):
        def boom(*a, **k):
            raise RuntimeError("unreachable / blocked / bad key")
        oc.get_json = boom
        self.assertEqual(self._dynamic().list_models(), [])

    def test_failure_is_not_cached_so_it_retries(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("down")
            return {"data": [{"id": "gpt-oss-120b"}]}
        oc.get_json = flaky
        p = self._dynamic()
        self.assertEqual(p.list_models(), [])                 # first call failed → empty
        self.assertEqual(p.list_models(), ["gpt-oss-120b"])   # retried (not cached) → live list

    def test_static_provider_still_uses_its_list(self):
        # dynamic_models=False is a deliberate fixed list (a provider without a /models
        # endpoint), not a failure fallback — that path is unchanged.
        p = OpenAICompatProvider("fixed", "https://x/v1", "FAKE_KEY", models=["m1", "m2"], dynamic_models=False)
        self.assertEqual(p.list_models(), ["m1", "m2"])


class NativeProviderListing(unittest.TestCase):
    """Google and Anthropic list live from their own /models endpoints, same contract:
    [] on failure (no stale hardcoded names — a retired model is worse than none), uncached."""

    def setUp(self):
        self._orig = (g.get_json, an.get_json)
        self._env = {k: os.environ.pop(k, None) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY")}
        os.environ["GEMINI_API_KEY"] = os.environ["ANTHROPIC_API_KEY"] = "k"

    def tearDown(self):
        g.get_json, an.get_json = self._orig
        for k, v in self._env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_google_lists_gemini_chat_models_only(self):
        seen = {}

        def fake(url, headers=None, **k):
            seen["url"], seen["headers"] = url, headers
            return {"models": [
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent", "countTokens"]},
                {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemma-3-27b-it", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-transcribe", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-computer-use-preview-10-2025", "supportedGenerationMethods": ["generateContent"]},
            ]}
        g.get_json = fake
        self.assertEqual(GoogleProvider().list_models(), ["gemini-2.5-pro", "gemini-3.8-flash"])
        self.assertEqual(seen["headers"], {"x-goog-api-key": "k"})   # key in a header, never the URL
        self.assertNotIn("key=", seen["url"])

    def test_anthropic_lists_live_models(self):
        seen = {}

        def fake(url, headers=None, **k):
            seen["headers"] = headers
            return {"data": [{"id": "claude-sonnet-5"}, {"id": "claude-haiku-4-5-20251001"}]}
        an.get_json = fake
        self.assertEqual(AnthropicProvider().list_models(), ["claude-haiku-4-5-20251001", "claude-sonnet-5"])
        self.assertEqual(seen["headers"]["x-api-key"], "k")

    def test_failure_returns_empty_and_retries(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("down")
            return {"models": [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]}]}
        g.get_json = flaky
        p = GoogleProvider()
        self.assertEqual(p.list_models(), [])                   # failed → empty, not a stale guess
        self.assertEqual(p.list_models(), ["gemini-3.8-flash"])  # not cached → retried

    def test_success_is_cached(self):
        calls = {"n": 0}

        def once(*a, **k):
            calls["n"] += 1
            return {"data": [{"id": "claude-sonnet-5"}]}
        an.get_json = once
        p = AnthropicProvider()
        p.list_models()
        p.list_models()
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
