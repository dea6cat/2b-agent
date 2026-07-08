"""AnthropicProvider.stream parses the Messages SSE stream: text deltas flow to
on_text, tool_use blocks assemble from input_json_delta fragments, and usage/stop
are captured. cancel is honored between events.

Run: `python -m unittest tests.test_anthropic_streaming` from the repo root.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers import anthropic as anth  # noqa: E402
from two_b.providers.base import _Cancelled  # noqa: E402

# A minimal but representative Messages SSE stream: one text block, then one
# tool_use block whose JSON arrives in two fragments.
_SSE = [
    'event: message_start\n',
    'data: {"type":"message_start","message":{"usage":{"input_tokens":42}}}\n',
    '\n',
    'event: content_block_start\n',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n',
    '\n',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}\n',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}\n',
    'data: {"type":"content_block_stop","index":0}\n',
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu_1","name":"read_file"}}\n',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}\n',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"a.py\\"}"}}\n',
    'data: {"type":"content_block_stop","index":1}\n',
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}\n',
    'data: {"type":"message_stop"}\n',
]


class Streaming(unittest.TestCase):
    def _patch_stream(self, lines):
        real = anth.post_stream
        anth.post_stream = lambda *a, **k: iter(lines)
        self.addCleanup(setattr, anth, "post_stream", real)

    def test_text_and_tool_use_are_parsed(self):
        self._patch_stream(_SSE)
        chunks = []
        conv = _FakeConv()
        resp = anth.AnthropicProvider().stream(conv, "claude-opus-4-8", (), chunks.append)
        self.assertEqual("".join(chunks), "Hello")
        self.assertEqual(resp.message.text, "Hello")
        self.assertEqual(len(resp.message.tool_calls), 1)
        tc = resp.message.tool_calls[0]
        self.assertEqual(tc.name, "read_file")
        self.assertEqual(tc.id, "tu_1")
        self.assertEqual(tc.arguments, {"path": "a.py"})
        self.assertEqual(resp.prompt_tokens, 42)
        self.assertEqual(resp.done_reason, "tool_use")

    def test_cancel_between_events_raises(self):
        cancel = threading.Event()

        def gen():
            yield 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
            yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n'
            cancel.set()
            # A real post_stream would raise _Cancelled itself; emulate that here.
            raise _Cancelled()

        real = anth.post_stream
        anth.post_stream = lambda *a, **k: gen()
        self.addCleanup(setattr, anth, "post_stream", real)
        with self.assertRaises(_Cancelled):
            anth.AnthropicProvider().stream(_FakeConv(), "m", (), lambda c: None, cancel=cancel)


class _FakeConv:
    system_prompt = "sys"
    messages = []


if __name__ == "__main__":
    unittest.main()
