"""The Google provider sends its API key via the x-goog-api-key header (as the official
google-genai SDK / Google guidance do), not a ?key= URL param — so the secret never lands
in a URL. Pure host-side (stdlib urllib), no SDK.
Run: `python -m unittest tests.test_google_key_header`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.conversation import Conversation, Message  # noqa: E402
from two_b.providers import google as g  # noqa: E402


class GoogleKeyHeader(unittest.TestCase):
    def setUp(self):
        self._orig_post = g.post_json
        self._orig_key = os.environ.get("GEMINI_API_KEY")
        self.captured = {}

        def fake_post_json(url, payload, headers=None, provider="http", **kw):
            self.captured["url"] = url
            self.captured["headers"] = headers or {}
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

        g.post_json = fake_post_json
        os.environ["GEMINI_API_KEY"] = "AIza-super-secret"

    def tearDown(self):
        g.post_json = self._orig_post
        if self._orig_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = self._orig_key

    def _send(self):
        conv = Conversation(system_prompt="sys")
        conv.append(Message.user("hi"))
        g.GoogleProvider().send(conv, "gemini-2.5-flash", ())

    def test_key_is_in_header(self):
        self._send()
        self.assertEqual(self.captured["headers"].get("x-goog-api-key"), "AIza-super-secret")

    def test_key_is_not_in_url(self):
        self._send()
        self.assertNotIn("key=", self.captured["url"])
        self.assertNotIn("AIza-super-secret", self.captured["url"])


if __name__ == "__main__":
    unittest.main()
