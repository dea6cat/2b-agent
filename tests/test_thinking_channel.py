"""stream_with_retry forwards on_thinking to provider.stream; THINKING_DELTA event exists.
Run: `python -m unittest tests.test_thinking_channel`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers.base import stream_with_retry  # noqa: E402
from two_b.orchestrator import EventType  # noqa: E402


class _FakeProvider:
    def __init__(self):
        self.seen = {}

    def stream(self, conv, model, tools, on_text, *, cancel=None, reasoning=None, on_thinking=None):
        self.seen["on_thinking"] = on_thinking
        if on_thinking:
            on_thinking("reasoning chunk")
        from two_b.conversation import Message
        from two_b.providers.base import ProviderResponse
        return ProviderResponse(message=Message.assistant(text="ok"), raw={})


class Channel(unittest.TestCase):
    def test_event_type_exists(self):
        self.assertEqual(EventType.THINKING_DELTA.value, "thinking_delta")

    def test_on_thinking_forwarded_and_invoked(self):
        got = []
        p = _FakeProvider()
        stream_with_retry(p, None, "m", (), lambda _c: None, on_thinking=got.append)
        self.assertEqual(got, ["reasoning chunk"])

    def test_default_on_thinking_is_none(self):
        p = _FakeProvider()
        stream_with_retry(p, None, "m", (), lambda _c: None)
        self.assertIsNone(p.seen["on_thinking"])


if __name__ == "__main__":
    unittest.main()
