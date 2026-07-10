"""Tests for P17's orchestrator half: dangling-reference detection, recall-term
extraction, archive injection into the live conversation, the compaction breadcrumb,
and tool-exchange integrity in the kept tail. Host-side. Run:
`python -m unittest tests.test_archive_inject`.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator, persist  # noqa: E402
from two_b.conversation import Conversation, Message, ToolCall, ToolResult, Role  # noqa: E402


class DanglingDetection(unittest.TestCase):
    def test_backward_references_match(self):
        for s in ["fix that file you edited", "the error from before", "go back to the parser",
                  "as I mentioned earlier", "remember the Lexer class", "same as before"]:
            self.assertTrue(orchestrator._DANGLING_RE.search(s), s)

    def test_forward_requests_do_not_match(self):
        for s in ["add a new getter to Account", "write a test for the sum function",
                  "create a config file"]:
            self.assertFalse(orchestrator._DANGLING_RE.search(s), s)


class RecallTerms(unittest.TestCase):
    def test_extracts_identifiers_and_paths_drops_generic(self):
        terms = orchestrator._recall_terms("fix that error in parser.dart around parseExpr again")
        self.assertIn("parser.dart", terms)
        self.assertIn("parseExpr", terms)
        self.assertNotIn("that", terms)      # stopword
        self.assertNotIn("error", terms)     # too generic
        self.assertNotIn("again", terms)     # reference vocabulary

    def test_dedup_and_cap(self):
        terms = orchestrator._recall_terms(" ".join(f"symbolNumber{i}" for i in range(20)) + " symbolNumber0")
        self.assertLessEqual(len(terms), 8)
        self.assertEqual(len(terms), len(set(t.lower() for t in terms)))


class Injection(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        os.environ["TWOB_HISTORY_DB"] = self.db.name
        os.environ.pop("TWOB_NO_HISTORY", None)
        self.addCleanup(lambda: os.environ.pop("TWOB_HISTORY_DB", None))
        self.addCleanup(lambda: os.path.exists(self.db.name) and os.unlink(self.db.name))
        # Seed the archive for task "t1" in this project.
        persist.archive_messages("t1", "/proj/a", [
            Message.assistant(text="editing",
                              tool_calls=[ToolCall.new("edit_file", {"path": "parser.dart"}, id="c1")]),
            Message.results([ToolResult(tool_call_id="c1", content="renamed parseExpr to parseExpression")]),
        ])

    def test_dangling_reference_merges_recall_into_latest_turn(self):
        conv = Conversation(system_prompt="sys")
        conv.append(Message.user("now revert that parseExpr rename you did earlier"))
        injected = orchestrator._maybe_inject_recall(conv, "t1", "/proj/a")
        self.assertTrue(injected)
        # Merged INTO the last turn (no new message) so no consecutive same-role turns arise.
        self.assertEqual(len(conv.messages), 1)
        text = conv.messages[-1].text
        self.assertTrue(text.startswith(orchestrator._RECALL_PREFIX))
        self.assertIn("parseExpression", text)      # the archived detail is back
        self.assertIn("revert", text)               # user's request preserved, after the recall

    @staticmethod
    def _consecutive_user_pairs(conv):
        r = [m.role for m in conv.messages]
        return sum(1 for i in range(len(r) - 1) if r[i] == Role.USER and r[i + 1] == Role.USER)

    def test_recall_does_not_add_a_new_consecutive_user_turn(self):
        # The resume path can end on a user-role tool-results turn (a pre-existing adjacency).
        # Recall must MERGE into the latest turn, not insert a new user message on top — which
        # would create an additional consecutive user pair that Gemini rejects.
        conv = Conversation(system_prompt="sys")
        conv.append(Message.assistant(tool_calls=[ToolCall.new("read_file", {"path": "x"}, id="c9")]))
        conv.append(Message.results([ToolResult(tool_call_id="c9", content="stuff")]))
        conv.append(Message.user("revert the parseExpr rename from earlier"))
        before_len = len(conv.messages)
        before_pairs = self._consecutive_user_pairs(conv)
        orchestrator._maybe_inject_recall(conv, "t1", "/proj/a")
        self.assertEqual(len(conv.messages), before_len)            # merged, not inserted
        self.assertEqual(self._consecutive_user_pairs(conv), before_pairs)   # no NEW adjacency

    def test_forward_request_does_not_inject(self):
        conv = Conversation(system_prompt="sys")
        conv.append(Message.user("add a brand new helper to parser.dart"))
        self.assertFalse(orchestrator._maybe_inject_recall(conv, "t1", "/proj/a"))
        self.assertEqual(len(conv.messages), 1)

    def test_dangling_but_no_archive_match_does_not_inject(self):
        conv = Conversation(system_prompt="sys")
        conv.append(Message.user("fix that thing you did earlier"))   # dangling, but no salient term hits
        self.assertFalse(orchestrator._maybe_inject_recall(conv, "t1", "/proj/a"))
        self.assertEqual(len(conv.messages), 1)

    def test_disabled_history_skips_injection(self):
        os.environ["TWOB_NO_HISTORY"] = "1"
        self.addCleanup(lambda: os.environ.pop("TWOB_NO_HISTORY", None))
        conv = Conversation(system_prompt="sys")
        conv.append(Message.user("revert the parseExpr rename from earlier"))
        self.assertFalse(orchestrator._maybe_inject_recall(conv, "t1", "/proj/a"))


class TailIntegrity(unittest.TestCase):
    def test_strips_leading_orphan_result_turn(self):
        # A result turn whose call was folded into the head must not lead the kept tail.
        orphan = Message.results([ToolResult(tool_call_id="gone", content="x")])
        good = Message.assistant(text="hi")
        out = orchestrator._strip_leading_orphan_results([orphan, good])
        self.assertEqual(out, [good])

    def test_keeps_intact_tail_untouched(self):
        a = Message.assistant(tool_calls=[ToolCall.new("read_file", {"path": "p"}, id="c1")])
        r = Message.results([ToolResult(tool_call_id="c1", content="body")])
        out = orchestrator._strip_leading_orphan_results([a, r])
        self.assertEqual(out, [a, r])

    def test_plain_user_turn_is_not_treated_as_orphan(self):
        u = Message.user("a real question")
        out = orchestrator._strip_leading_orphan_results([u])
        self.assertEqual(out, [u])


class _FakeSummarizer:
    def stream(self, conv, model, tools, on_text, *, cancel=None, **_kwargs):
        from two_b.providers.base import ProviderResponse
        on_text("GOAL: x\nDONE:\n1. did a\nOUTSTANDING: y\nSTATE: z")
        return ProviderResponse(message=Message.assistant(text="ok"), raw={})


class CompactArchivesAndBreadcrumbs(unittest.TestCase):
    def test_dropped_turns_returned_and_breadcrumb_appended(self):
        conv = Conversation(system_prompt="sys")
        for i in range(12):
            conv.append(Message.assistant(tool_calls=[ToolCall.new("read_file", {"path": f"f{i}"}, id=f"c{i}")]))
            conv.append(Message.results([ToolResult(tool_call_id=f"c{i}", content=f"body {i}")]))
        dropped = orchestrator.compact_conversation(conv, _FakeSummarizer(), "m",
                                                    breadcrumb=orchestrator._ARCHIVE_BREADCRUMB)
        self.assertTrue(dropped)                                # returns the folded-away turns
        self.assertIn("archived", conv.messages[0].text)        # breadcrumb present in the recap
        # The recap replaced the head; the tail is intact call/result pairs.
        for i, m in enumerate(conv.messages):
            if m.tool_calls:
                self.assertTrue(conv.messages[i + 1].tool_results, f"orphaned call at {i}")


if __name__ == "__main__":
    unittest.main()
