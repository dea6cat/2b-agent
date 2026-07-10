"""Tests for the P22/P27 compaction cluster: structured summary template, iterative
update, attachment hint, and token-estimate calibration. Host-side. Run:
`python -m unittest tests.test_compaction_hardening`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402
from two_b.conversation import Conversation, Message, ToolCall, ToolResult  # noqa: E402
from two_b.providers.base import ProviderResponse  # noqa: E402
from two_b.session import Task  # noqa: E402


class _FakeSummarizer:
    """Captures the summarization request it's handed and returns a canned summary."""
    def __init__(self, out="GOAL: build x\nDONE:\n1. did a\nOUTSTANDING: y\nSTATE: z"):
        self.out, self.seen_system, self.seen_input = out, None, None
        self.seen_reasoning = "unset"

    def stream(self, conv, model, tools, on_text, *, cancel=None, **_kwargs):
        self.seen_system = conv.system_prompt
        self.seen_input = conv.messages[0].text if conv.messages else ""
        self.seen_reasoning = _kwargs.get("reasoning")
        on_text(self.out)
        return ProviderResponse(message=Message.assistant(text=self.out), raw={})


def _long_conv(system="sys", prior_recap=None):
    conv = Conversation(system_prompt=system)
    if prior_recap is not None:
        conv.append(Message.user(orchestrator._RECAP_PREFIX + prior_recap))
    for i in range(12):
        conv.append(Message.assistant(tool_calls=[ToolCall.new("read_file", {"path": f"f{i}.py"})]))
        conv.append(Message.results([ToolResult(tool_call_id="x", content=f"body {i}")]))
    return conv


class Template(unittest.TestCase):
    def test_summary_prompt_is_structured_and_reference_only(self):
        s = orchestrator.COMPACT_SYSTEM
        for section in ("GOAL", "DONE", "OUTSTANDING", "STATE"):
            self.assertIn(section, s)
        self.assertIn("REFERENCE ONLY", s)
        self.assertIn("latest message always takes priority", s)


class Compact(unittest.TestCase):
    def test_fresh_summary_writes_a_recap_with_attachment_hint(self):
        conv = _long_conv()
        prov = _FakeSummarizer()
        self.assertTrue(orchestrator.compact_conversation(conv, prov, "m", touched=["a.py", "b.py"]))
        recap = conv.messages[0].text
        self.assertTrue(recap.startswith(orchestrator._RECAP_PREFIX))
        self.assertIn("did a", recap)
        self.assertIn("Recently-touched files: a.py, b.py", recap)
        self.assertNotIn(orchestrator.COMPACT_UPDATE, prov.seen_system)   # fresh: no update instruction

    def test_compaction_runs_with_reasoning_off(self):
        # Summarization never needs thinking — compaction forces reasoning="off".
        conv = _long_conv()
        prov = _FakeSummarizer()
        orchestrator.compact_conversation(conv, prov, "m")
        self.assertEqual(prov.seen_reasoning, "off")

    def test_iterative_update_feeds_prior_summary(self):
        conv = _long_conv(prior_recap="GOAL: build x\nDONE:\n1. earlier thing\nOUTSTANDING: rest")
        prov = _FakeSummarizer()
        self.assertTrue(orchestrator.compact_conversation(conv, prov, "m"))
        # The summarizer was told to update in place, and given the prior summary + new turns.
        self.assertIn(orchestrator.COMPACT_UPDATE, prov.seen_system)
        self.assertIn("PREVIOUS SUMMARY", prov.seen_input)
        self.assertIn("earlier thing", prov.seen_input)
        self.assertIn("NEW TURNS SINCE", prov.seen_input)

    def test_cut_lands_on_assistant_boundary_keeping_pairs(self):
        conv = _long_conv()
        orchestrator.compact_conversation(conv, _FakeSummarizer(), "m")
        # After the recap, every tool_call message is still followed by its results.
        msgs = conv.messages
        for i, m in enumerate(msgs):
            if m.tool_calls:
                self.assertTrue(i + 1 < len(msgs) and msgs[i + 1].tool_results,
                                f"orphaned tool_call at {i}")

    def test_empty_summary_aborts(self):
        conv = _long_conv()
        prov = _FakeSummarizer(out="")
        self.assertFalse(orchestrator.compact_conversation(conv, prov, "m"))

    def test_iterative_with_no_new_turns_returns_false(self):
        # Only a prior recap sits ahead of the tail — nothing new to fold. Must not
        # re-summarize the recap into itself or falsely report a shrink.
        conv = Conversation(system_prompt="sys")
        conv.append(Message.user(orchestrator._RECAP_PREFIX + "GOAL: x\nDONE:\n1. a"))
        conv.append(Message.assistant(tool_calls=[ToolCall.new("read_file", {"path": "a"})]))
        conv.append(Message.results([ToolResult(tool_call_id="x", content="y" * 8000)]))
        prov = _FakeSummarizer()
        self.assertFalse(orchestrator.compact_conversation(conv, prov, "m"))
        self.assertIsNone(prov.seen_input)          # summarizer never called

    def test_prior_attachment_hint_is_not_re_fed(self):
        # The old "Recently-touched files" line must be stripped before re-feeding, so hints
        # don't accumulate across compaction cycles.
        conv = _long_conv(prior_recap="GOAL: x\nDONE:\n1. old\n\nRecently-touched files: old.py")
        prov = _FakeSummarizer()
        orchestrator.compact_conversation(conv, prov, "m")
        self.assertIn("GOAL: x", prov.seen_input)
        self.assertNotIn("Recently-touched files: old.py", prov.seen_input)


class AttachmentHint(unittest.TestCase):
    def test_dedup_and_empty(self):
        self.assertEqual(orchestrator._attachment_hint([]), "")
        self.assertEqual(orchestrator._attachment_hint(["a.py", "a.py", "b.py"]),
                         "\n\nRecently-touched files: a.py, b.py")


class Calibration(unittest.TestCase):
    def test_estimate_scales_with_ratio(self):
        conv = Conversation(system_prompt="x" * 400)
        self.assertEqual(orchestrator.estimate_tokens(conv, 4.0), 100)
        self.assertEqual(orchestrator.estimate_tokens(conv, 2.0), 200)   # denser tokenizer → more tokens

    def test_calibrate_moves_toward_observed_and_clamps(self):
        t = Task(description="x")
        conv = Conversation(system_prompt="c" * 300)     # 300 chars
        # Provider reports 150 prompt tokens -> observed 2.0 chars/token.
        orchestrator._calibrate(t, conv, 150)
        self.assertLess(t.chars_per_token, 4.0)          # moved down from the 4.0 default
        self.assertGreaterEqual(t.chars_per_token, 2.0)  # clamp floor
        # Repeated calibration converges toward the observed 2.0.
        for _ in range(20):
            orchestrator._calibrate(t, conv, 150)
        self.assertAlmostEqual(t.chars_per_token, 2.0, places=1)

    def test_tiny_prompt_is_ignored(self):
        t = Task(description="x")
        before = t.chars_per_token
        orchestrator._calibrate(t, Conversation(system_prompt="x" * 30), 5)   # <20 tokens
        self.assertEqual(t.chars_per_token, before)


if __name__ == "__main__":
    unittest.main()
