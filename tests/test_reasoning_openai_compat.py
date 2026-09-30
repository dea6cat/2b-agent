"""OpenAI-compatible services each control reasoning with their OWN fields (no shared shim):
OpenAI/Cerebras `reasoning_effort`, OpenRouter a `reasoning` object, DeepSeek `thinking` +
`reasoning_effort`. Reasoning text streams as `delta.reasoning_content` (DeepSeek) or
`delta.reasoning` (OpenRouter, Cerebras), and DeepSeek must get its reasoning_content back on
later assistant turns when tools are in play. Run: `python -m unittest tests.test_reasoning_openai_compat`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.core.conversation import Conversation, Message, ToolCall  # noqa: E402
from two_b.providers import openai_compat as oc, registry  # noqa: E402
from two_b.providers.openai_compat import OpenAICompatProvider  # noqa: E402


def _p(style, **kw):
    return OpenAICompatProvider("x", "https://x/v1", "K", reasoning_style=style, **kw)


class ReasoningFields(unittest.TestCase):
    def test_capability(self):
        self.assertTrue(_p("openai").supports_reasoning("gpt-5.2"))
        self.assertTrue(_p("openai").supports_reasoning("o4-mini"))
        self.assertFalse(_p("openai").supports_reasoning("gpt-4o"))      # would 400 on reasoning_effort
        self.assertTrue(_p("openrouter").supports_reasoning("anything"))  # ignored by non-reasoning models
        self.assertTrue(_p("deepseek").supports_reasoning("deepseek-flash"))
        self.assertTrue(_p("cerebras").supports_reasoning("gpt-oss-120b"))
        self.assertFalse(_p("cerebras").supports_reasoning("llama-3.3-70b"))
        self.assertFalse(_p(None).supports_reasoning("mistral-small-latest"))

    def test_none_leaves_the_provider_default(self):
        for style in ("openai", "openrouter", "deepseek", "cerebras"):
            self.assertEqual(_p(style)._reasoning_fields("gpt-5.2" if style == "openai" else "gpt-oss-120b", None), {})

    def test_openai(self):
        f = _p("openai")._reasoning_fields
        self.assertEqual(f("gpt-5.2", "high"), {"reasoning_effort": "high"})
        self.assertEqual(f("gpt-5.2", "off"), {"reasoning_effort": "low"})   # lowest every reasoning model takes
        self.assertEqual(f("gpt-5.2", "on"), {})
        self.assertEqual(f("gpt-4o", "high"), {})

    def test_openrouter(self):
        f = _p("openrouter")._reasoning_fields
        self.assertEqual(f("m", "medium"), {"reasoning": {"effort": "medium"}})
        self.assertEqual(f("m", "off"), {"reasoning": {"effort": "none"}})
        self.assertEqual(f("m", "on"), {"reasoning": {"enabled": True}})

    def test_deepseek(self):
        f = _p("deepseek")._reasoning_fields
        self.assertEqual(f("deepseek-flash", "off"), {"thinking": {"type": "disabled"}})
        self.assertEqual(f("deepseek-flash", "low"), {"thinking": {"type": "enabled"}, "reasoning_effort": "low"})
        self.assertEqual(f("deepseek-flash", "medium"), {"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
        self.assertEqual(f("deepseek-flash", "on"), {"thinking": {"type": "enabled"}})

    def test_cerebras_off_depends_on_the_model(self):
        f = _p("cerebras")._reasoning_fields
        self.assertEqual(f("qwen-3.8-27b", "off"), {"reasoning_effort": "none"})
        self.assertEqual(f("gpt-oss-120b", "off"), {"reasoning_effort": "low"})   # gpt-oss can't disable
        self.assertEqual(f("gpt-oss-120b", "high"), {"reasoning_effort": "high"})


class Payload(unittest.TestCase):
    def setUp(self):
        self._orig = oc.post_stream
        self.sent = {}

        def fake(url, payload, **kw):
            self.sent.update(payload)
            return iter([
                'data: {"choices":[{"delta":{"reasoning_content":"think "}}]}\n',
                'data: {"choices":[{"delta":{"reasoning":"more"}}]}\n',
                'data: {"choices":[{"delta":{"content":"answer"}}]}\n',
                "data: [DONE]\n",
            ])
        oc.post_stream = fake

    def tearDown(self):
        oc.post_stream = self._orig

    def _run(self, p, model="m", reasoning=None):
        conv = Conversation(system_prompt="s")
        conv.append(Message.user("hi"))
        thoughts = []
        resp = p.stream(conv, model, (), lambda c: None, reasoning=reasoning, on_thinking=thoughts.append)
        return resp, thoughts

    def test_reasoning_text_streams_separately_from_the_answer(self):
        resp, thoughts = self._run(_p("openrouter"))
        self.assertEqual(thoughts, ["think ", "more"])
        self.assertEqual(resp.message.thinking, "think more")
        self.assertEqual(resp.message.text, "answer")

    def test_fields_and_max_tokens_reach_the_request(self):
        self._run(_p("openrouter", max_tokens=16384), reasoning="high")
        self.assertEqual(self.sent["reasoning"], {"effort": "high"})
        self.assertEqual(self.sent["max_tokens"], 16384)

    def test_no_max_tokens_unless_configured(self):
        self._run(_p(None))
        self.assertNotIn("max_tokens", self.sent)
        self.assertNotIn("reasoning", self.sent)


class DeepSeekReplay(unittest.TestCase):
    def test_reasoning_content_is_sent_back_only_to_deepseek(self):
        conv = Conversation(system_prompt="s")
        conv.append(Message.user("hi"))
        conv.append(Message.assistant(thinking="because", tool_calls=[ToolCall(id="1", name="read_file", arguments={})]))
        ds = [m for m in _p("deepseek")._messages(conv) if m["role"] == "assistant"][0]
        self.assertEqual(ds["reasoning_content"], "because")
        other = [m for m in _p("openrouter")._messages(conv) if m["role"] == "assistant"][0]
        self.assertNotIn("reasoning_content", other)


class RegistryData(unittest.TestCase):
    def test_styles_and_caps_are_configured_as_data(self):
        reg = registry.build_registry()
        self.assertEqual(reg["openai"]._reasoning_style, "openai")
        self.assertEqual(reg["openrouter"]._reasoning_style, "openrouter")
        self.assertEqual(reg["openrouter"]._max_tokens, 16384)
        self.assertEqual(reg["deepseek"]._reasoning_style, "deepseek")
        self.assertEqual(reg["cerebras"]._reasoning_style, "cerebras")
        self.assertIsNone(reg["mistral"]._reasoning_style)
        self.assertIsNone(reg["deepseek"]._max_tokens)


if __name__ == "__main__":
    unittest.main()
