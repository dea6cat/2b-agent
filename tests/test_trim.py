import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from two_b import conversation as C
from two_b.conversation import Conversation, Message, ToolResult
class Trim(unittest.TestCase):
    def test_old_large_result_elided_recent_kept(self):
        big = "x" * 5000
        conv = Conversation(system_prompt="s", messages=[
            Message.user("go"),
            Message.results([ToolResult("id1", big)]),        # old + large -> elided
            *[Message.user(f"m{i}") for i in range(6)],        # 6 recent messages
        ])
        out = C.trimmed(conv, keep_recent=6, max_chars=2000)
        self.assertIn("elided", out.messages[1].tool_results[0].content)
        self.assertEqual(len(out.messages), len(conv.messages))     # structure preserved
        self.assertEqual(conv.messages[1].tool_results[0].content, big)  # original NOT mutated
    def test_small_and_recent_results_untouched(self):
        conv = Conversation(system_prompt="s", messages=[
            Message.results([ToolResult("id", "small")]),
            *[Message.user(f"m{i}") for i in range(6)],
        ])
        out = C.trimmed(conv, keep_recent=6, max_chars=2000)
        self.assertEqual(out.messages[0].tool_results[0].content, "small")
