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


if __name__ == "__main__":
    unittest.main()
