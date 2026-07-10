"""Google.stream separates Gemini `thought` parts to on_thinking; send() never merges them.
Run: `python -m unittest tests.test_thinking_google`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.conversation import Conversation  # noqa: E402
from two_b.providers import google as g  # noqa: E402

_SSE = [
    'data: {"candidates":[{"content":{"parts":[{"text":"planning: guard null","thought":true}]}}]}\n',
    'data: {"candidates":[{"content":{"parts":[{"text":"Here is"}]}}]}\n',
    'data: {"candidates":[{"content":{"parts":[{"text":" the fix"}]}}]}\n',
]


class GoogleThinking(unittest.TestCase):
    def setUp(self):
        self._orig = g.post_stream
        self._captured = {}

        def fake(url, payload, headers=None, provider="http", **kw):
            self._captured["payload"] = payload
            return iter(_SSE)

        g.post_stream = fake
        self._key = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "AIza-key"
        self.addCleanup(lambda: setattr(g, "post_stream", self._orig))
        self.addCleanup(lambda: os.environ.__setitem__("GEMINI_API_KEY", self._key) if self._key else os.environ.pop("GEMINI_API_KEY", None))

    def test_thought_parts_routed_and_answer_separate(self):
        prov = g.GoogleProvider()
        thoughts, reply = [], []
        resp = prov.stream(Conversation(system_prompt="s"), "gemini-2.5-flash", (),
                           reply.append, on_thinking=thoughts.append)
        self.assertIn("planning: guard null", "".join(thoughts))
        self.assertEqual("".join(reply), "Here is the fix")
        self.assertEqual(resp.message.text, "Here is the fix")
        self.assertEqual(resp.message.thinking, "planning: guard null")
        # includeThoughts requested for a reasoning-capable model
        self.assertTrue(self._captured["payload"]["generationConfig"]["thinkingConfig"]["includeThoughts"])

    def test_send_does_not_merge_thoughts_into_reply(self):
        prov = g.GoogleProvider()
        # _read_parts without a thought sink must drop thought parts from the answer text.
        text_parts, calls = [], []
        cand = {"content": {"parts": [{"text": "THINK", "thought": True}, {"text": "ANSWER"}]}}
        prov._read_parts(cand, text_parts, calls)
        self.assertEqual("".join(text_parts), "ANSWER")

    def test_think_off_does_not_request_thoughts_even_on_pro(self):
        # `/think off` must not surface thoughts, even on 2.5 Pro (whose budget floors at 128) —
        # otherwise the off gating is contradicted and includeThoughts rides a 0/low budget.
        prov = g.GoogleProvider()
        prov.stream(Conversation(system_prompt="s"), "gemini-2.5-pro", (), lambda _c: None, reasoning="off")
        tc = self._captured["payload"]["generationConfig"]["thinkingConfig"]
        self.assertNotIn("includeThoughts", tc)


if __name__ == "__main__":
    unittest.main()
