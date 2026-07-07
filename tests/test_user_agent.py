"""Every provider request carries a real User-Agent. urllib's default "Python-urllib/x" is
blocked by some bot filters (Cerebras's Cloudflare -> 403 "error code: 1010"), so base's
HTTP helpers must send "2b-agent/<version>" instead. Pure host-side.
Run: `python -m unittest tests.test_user_agent`.
"""
import os
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers import base  # noqa: E402


class _Resp:
    def __init__(self, data=b'{"ok": true}'):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data

    def __iter__(self):
        return iter([self._data])


class UserAgent(unittest.TestCase):
    def setUp(self):
        self._orig = urllib.request.urlopen
        self.captured = {}

        def fake_urlopen(req, timeout=None):
            self.captured["req"] = req
            return _Resp()

        urllib.request.urlopen = fake_urlopen

    def tearDown(self):
        urllib.request.urlopen = self._orig

    def _ua(self):
        return self.captured["req"].get_header("User-agent")

    def test_post_json_sends_real_user_agent(self):
        base.post_json("https://example/api", {"a": 1})
        ua = self._ua()
        self.assertTrue(ua.startswith("2b-agent/"))
        self.assertNotIn("Python-urllib", ua)

    def test_get_json_sends_real_user_agent(self):
        base.get_json("https://example/api")
        self.assertTrue(self._ua().startswith("2b-agent/"))

    def test_post_stream_sends_real_user_agent(self):
        list(base.post_stream("https://example/api", {"a": 1}))   # consume the generator
        self.assertTrue(self._ua().startswith("2b-agent/"))

    def test_caller_headers_still_applied(self):
        base.post_json("https://example/api", {"a": 1}, headers={"x-goog-api-key": "k"})
        self.assertEqual(self.captured["req"].get_header("X-goog-api-key"), "k")
        self.assertTrue(self._ua().startswith("2b-agent/"))   # ...alongside the UA


if __name__ == "__main__":
    unittest.main()
