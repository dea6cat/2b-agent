"""GoogleProvider.stream parses Gemini SSE (:streamGenerateContent?alt=sse), emitting text
deltas as they arrive and collecting functionCall parts — stdlib only, no SDK.
Run: `python -m unittest tests.test_google_streaming`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.core.conversation import Conversation, Message  # noqa: E402
from two_b.providers import google as g  # noqa: E402

# post_stream yields decoded lines; Gemini SSE is `data: {chunk}` per event, blank line between.
_SSE_LINES = [
    'data: {"candidates":[{"content":{"parts":[{"text":"Hel"}]}}]}\n',
    "\n",
    'data: {"candidates":[{"content":{"parts":[{"text":"lo"}]}}]}\n',
    "data: {bad json here\n",   # malformed event must be skipped, not crash
    'data: {"candidates":[{"content":{"parts":[{"functionCall":{"name":"read_file","args":{"path":"a.py"}},'
    '"thoughtSignature":"SIG-a"}]}}]}\n',
]


class GoogleStreaming(unittest.TestCase):
    def setUp(self):
        self._orig_stream = g.post_stream
        self._orig_key = os.environ.get("GEMINI_API_KEY")
        self.captured = {}

        def fake_post_stream(url, payload, headers=None, provider="http", **kw):
            self.captured["url"] = url
            self.captured["headers"] = headers or {}
            return iter(_SSE_LINES)

        g.post_stream = fake_post_stream
        os.environ["GEMINI_API_KEY"] = "AIza-key"

    def tearDown(self):
        g.post_stream = self._orig_stream
        if self._orig_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = self._orig_key

    def _stream(self):
        conv = Conversation(system_prompt="sys")
        conv.append(Message.user("hi"))
        deltas = []
        resp = g.GoogleProvider().stream(conv, "gemini-2.5-flash", (), deltas.append)
        return deltas, resp

    def test_text_is_emitted_incrementally_and_assembled(self):
        deltas, resp = self._stream()
        self.assertEqual(deltas, ["Hel", "lo"])          # streamed as it arrived
        self.assertEqual(resp.message.text, "Hello")     # and assembled in the final message

    def test_function_calls_are_collected(self):
        _deltas, resp = self._stream()
        self.assertEqual(len(resp.message.tool_calls), 1)
        tc = resp.message.tool_calls[0]
        self.assertEqual(tc.name, "read_file")
        self.assertEqual(tc.arguments, {"path": "a.py"})

    def test_thought_signature_is_kept_on_the_call(self):
        # Gemini 3 400s a replayed function call that lacks its thoughtSignature.
        _deltas, resp = self._stream()
        self.assertEqual(resp.message.tool_calls[0].signature, "SIG-a")

    def test_signatures_are_echoed_back_and_missing_ones_use_the_placeholder(self):
        from two_b.core.conversation import ToolCall
        conv = Conversation(system_prompt="sys")
        conv.append(Message.user("hi"))
        conv.append(Message.assistant(tool_calls=[
            ToolCall(id="1", name="read_file", arguments={"path": "a"}, signature="SIG-a"),
            ToolCall(id="2", name="read_file", arguments={"path": "b"})]))   # e.g. from another model
        parts = g.GoogleProvider()._contents(conv)[1]["parts"]
        self.assertEqual([p["thoughtSignature"] for p in parts], ["SIG-a", g._SKIP_SIGNATURE])

    def test_uses_sse_endpoint_with_header_key(self):
        self._stream()
        self.assertIn("streamGenerateContent", self.captured["url"])
        self.assertIn("alt=sse", self.captured["url"])
        self.assertNotIn("key=", self.captured["url"])   # key not in the URL
        self.assertEqual(self.captured["headers"].get("x-goog-api-key"), "AIza-key")


if __name__ == "__main__":
    unittest.main()
