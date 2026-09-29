"""Anthropic maps /think to each model family's own controls: always-thinking models (Opus 5.5,
Sonnet 5.5, Fable 5.x) take only `output_config.effort`; adaptive models take `thinking` adaptive
or disabled plus effort; older budget-thinking models are left alone. Summarized thinking streams
to the thinking channel but is never replayed (2B trims old tool results each request, which
Anthropic treats as a history edit that would invalidate a replayed block).
Run: `python -m unittest tests.test_reasoning_anthropic`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.core.conversation import Conversation, Message  # noqa: E402
from two_b.providers import anthropic as an  # noqa: E402
from two_b.providers.anthropic import AnthropicProvider  # noqa: E402

SUMMARIZED = {"type": "adaptive", "display": "summarized"}


class Fields(unittest.TestCase):
    def setUp(self):
        self.f = AnthropicProvider()._thinking_fields

    def test_capability(self):
        p = AnthropicProvider()
        for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5",
                  "claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-4-6"):
            self.assertTrue(p.supports_reasoning(m), m)
        for m in ("claude-haiku-4-5-20251001", "claude-sonnet-4-5", "claude-3-opus"):
            self.assertFalse(p.supports_reasoning(m), m)

    def test_always_thinking_models_use_effort_only(self):
        self.assertEqual(self.f("claude-opus-5-5", None), {"thinking": SUMMARIZED})
        self.assertEqual(self.f("claude-opus-5-5", "high"), {"thinking": SUMMARIZED, "output_config": {"effort": "high"}})
        # can't disable: "off" (also the compaction path) is the lowest effort, reasoning not shown
        self.assertEqual(self.f("claude-sonnet-5-5", "off"),
                         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}})
        self.assertEqual(self.f("claude-fable-5-1", "on"), {"thinking": SUMMARIZED})

    def test_adaptive_models(self):
        self.assertEqual(self.f("claude-opus-4-8", None), {})                       # unchanged default
        self.assertEqual(self.f("claude-opus-4-8", "off"), {"thinking": {"type": "disabled"}})
        self.assertEqual(self.f("claude-opus-5", "medium"), {"thinking": SUMMARIZED, "output_config": {"effort": "medium"}})
        self.assertEqual(self.f("claude-sonnet-5", "on"), {"thinking": SUMMARIZED})
        # 4.6 already summarizes by default; only newer models need display set
        self.assertEqual(self.f("claude-sonnet-4-6", "low"),
                         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}})

    def test_other_models_are_untouched(self):
        self.assertEqual(self.f("claude-haiku-4-5", "high"), {})

    def test_output_cap_from_catalog(self):
        from two_b.providers import catalog
        self.assertEqual(catalog.max_tokens("claude-opus-5-5", 4096), 64000)
        self.assertEqual(catalog.max_tokens("claude-haiku-4-5-20251001", 4096), 64000)
        self.assertEqual(catalog.max_tokens("claude-3-opus-20240229", 64000), 4096)


class Streaming(unittest.TestCase):
    def setUp(self):
        self._orig = an.post_stream
        self.sent = {}

        def fake(url, payload, **kw):
            self.sent.update(payload)
            return iter([
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Plan: "}}\n',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"read it."}}\n',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig"}}\n',
                'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n',
                'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Done."}}\n',
                'data: {"type":"message_stop"}\n',
            ])
        an.post_stream = fake

    def tearDown(self):
        an.post_stream = self._orig

    def test_thinking_streams_to_the_thinking_channel_not_the_answer(self):
        conv = Conversation(system_prompt="s")
        conv.append(Message.user("hi"))
        text, thoughts = [], []
        resp = AnthropicProvider().stream(conv, "claude-opus-5-5", (), text.append, reasoning="high",
                                          on_thinking=thoughts.append)
        self.assertEqual(thoughts, ["Plan: ", "read it."])
        self.assertEqual(text, ["Done."])
        self.assertEqual(resp.message.thinking, "Plan: read it.")
        self.assertEqual(self.sent["output_config"], {"effort": "high"})
        self.assertEqual(self.sent["max_tokens"], 64000)

    def test_thinking_is_not_replayed(self):
        conv = Conversation(system_prompt="s")
        conv.append(Message.user("hi"))
        conv.append(Message.assistant(text="ok", thinking="secret plan"))
        blocks = AnthropicProvider()._messages(conv)[1]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text"])


if __name__ == "__main__":
    unittest.main()
