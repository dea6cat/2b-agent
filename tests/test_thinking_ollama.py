"""Ollama.stream emits thinking chunks live via on_thinking (and still returns Message.thinking).
Run: `python -m unittest tests.test_thinking_ollama`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.conversation import Conversation  # noqa: E402
from two_b.providers import ollama as o  # noqa: E402

_LINES = [
    '{"message":{"thinking":"let me "}}\n',
    '{"message":{"thinking":"check auth"}}\n',
    '{"message":{"content":"Here"}}\n',
    '{"message":{"content":" is the fix"}}\n',
    '{"done":true,"done_reason":"stop","prompt_eval_count":5}\n',
]


class OllamaThinking(unittest.TestCase):
    def setUp(self):
        self._orig = o.post_stream
        o.post_stream = lambda *a, **k: iter(_LINES)
        self.addCleanup(lambda: setattr(o, "post_stream", self._orig))

    def test_thinking_streamed_and_captured(self):
        prov = o.OllamaProvider()
        thoughts, reply = [], []
        resp = prov.stream(Conversation(system_prompt="s"), "qwen3.5:9b", (),
                           reply.append, on_thinking=thoughts.append)
        self.assertEqual("".join(thoughts), "let me check auth")   # streamed live
        self.assertEqual("".join(reply), "Here is the fix")        # reply separate
        self.assertEqual(resp.message.thinking, "let me check auth")  # still captured
        self.assertEqual(resp.message.text, "Here is the fix")

    def test_no_on_thinking_still_works(self):
        prov = o.OllamaProvider()
        resp = prov.stream(Conversation(system_prompt="s"), "qwen3.5:9b", (), lambda _c: None)
        self.assertEqual(resp.message.thinking, "let me check auth")


if __name__ == "__main__":
    unittest.main()
