"""stream_with_retry forwards `reasoning` to provider.stream; deferred providers report
supports_reasoning() False. Run: `python -m unittest tests.test_reasoning_plumbing`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers.base import stream_with_retry  # noqa: E402
from two_b.providers.anthropic import AnthropicProvider  # noqa: E402
from two_b.providers.openai_compat import OpenAICompatProvider  # noqa: E402


class _FakeProvider:
    def __init__(self):
        self.seen = {}

    def stream(self, conv, model, tools, on_text, *, cancel=None, reasoning=None):
        self.seen["reasoning"] = reasoning
        from two_b.conversation import Message
        from two_b.providers.base import ProviderResponse
        return ProviderResponse(message=Message.assistant(text="ok"), raw={})


class Plumbing(unittest.TestCase):
    def test_default_reasoning_is_none(self):
        p = _FakeProvider()
        stream_with_retry(p, None, "m", (), lambda _c: None)
        self.assertIsNone(p.seen["reasoning"])

    def test_reasoning_forwarded(self):
        p = _FakeProvider()
        stream_with_retry(p, None, "m", (), lambda _c: None, reasoning="off")
        self.assertEqual(p.seen["reasoning"], "off")

    def test_deferred_providers_report_unsupported(self):
        self.assertFalse(AnthropicProvider().supports_reasoning("claude-opus-4-8"))
        self.assertFalse(OpenAICompatProvider("x", "http://x", "K").supports_reasoning("m"))


if __name__ == "__main__":
    unittest.main()
