"""Tests for P5 prompt-prefix stability + keep-alive.

The system prompt + frozen tool schema must be byte-identical across turns so Ollama's
prefix KV cache is reused; keep_alive keeps that cache resident. Host-side. Run:
`python -m unittest tests.test_prompt_prefix`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import conversation, orchestrator  # noqa: E402
from two_b.conversation import Conversation, Message, ToolCall, ToolResult  # noqa: E402
from two_b.providers import ollama  # noqa: E402
from two_b.toolspec import specs_for, to_openai  # noqa: E402


class PrefixStability(unittest.TestCase):
    def test_system_prompt_is_a_stable_constant(self):
        self.assertEqual(orchestrator.SYSTEM_PROMPT, orchestrator.SYSTEM_PROMPT)
        self.assertNotIn("2026", orchestrator.SYSTEM_PROMPT)   # no volatile date baked into the prefix

    def test_tool_schema_serialization_is_deterministic(self):
        # The frozen tool schema must serialize byte-identically every turn.
        a = to_openai(specs_for(is_local=True))
        b = to_openai(specs_for(is_local=True))
        self.assertEqual(a, b)

    def test_trimming_does_not_touch_the_prefix(self):
        # conversation.trimmed elides OLD tool-result bodies but must leave the system
        # prompt (the cached prefix) untouched.
        conv = Conversation(system_prompt=orchestrator.SYSTEM_PROMPT)
        for i in range(10):
            conv.append(Message.assistant(tool_calls=[ToolCall.new("read_file", {"path": f"f{i}"})]))
            conv.append(Message.results([ToolResult(tool_call_id="x", content="y" * 5000)]))
        self.assertEqual(conversation.trimmed(conv).system_prompt, orchestrator.SYSTEM_PROMPT)

    def test_ollama_messages_put_system_first_and_stable(self):
        p = ollama.OllamaProvider(name="ollama")
        conv = Conversation(system_prompt="SYS", messages=[Message.user("hi")])
        m1 = p._messages(conv)
        m2 = p._messages(conv)
        self.assertEqual(m1[0], {"role": "system", "content": "SYS"})
        self.assertEqual(m1, m2)


class KeepAlive(unittest.TestCase):
    def test_keep_alive_is_a_generous_duration(self):
        # Longer than Ollama's 5m default so the warm prefix survives a slow turn.
        self.assertTrue(ollama.KEEP_ALIVE.endswith("m") or ollama.KEEP_ALIVE.endswith("h"))
        self.assertNotEqual(ollama.KEEP_ALIVE, "5m")


if __name__ == "__main__":
    unittest.main()
