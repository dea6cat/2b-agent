import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from two_b.providers import anthropic as A
from two_b.conversation import Conversation
from two_b.toolspec import TOOL_SPECS


class Cache(unittest.TestCase):
    def test_system_and_tools_cached(self):
        captured = {}
        A.post_json = lambda url, payload, **k: captured.setdefault("p", payload) or {"content": [{"type": "text", "text": "ok"}]}
        os.environ["ANTHROPIC_API_KEY"] = "x"
        A.AnthropicProvider().send(Conversation(system_prompt="SYS"), "claude-sonnet-5", tuple(TOOL_SPECS))
        p = captured["p"]
        self.assertEqual(p["system"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(p["tools"][-1]["cache_control"], {"type": "ephemeral"})
