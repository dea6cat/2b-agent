"""Tests for session persistence + conversation serialization (Phase 3).

Uses a temp DB via TWOB_HISTORY_DB, so nothing touches the real ~/.config/2b.
Run: `python -m unittest tests.test_persist` from the repo root.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import conversation as conv_mod, persist  # noqa: E402
from two_b.conversation import Conversation, Message, ToolCall, ToolResult, Role  # noqa: E402


def _sample_conv():
    c = Conversation(system_prompt="SYS")
    c.append(Message.user("add a getter"))
    c.append(Message.assistant(text="on it", thinking="hmm",
                               tool_calls=[ToolCall.new("edit_file", {"path": "a.dart", "old_text": "x", "new_text": "y"}, id="call_1")]))
    c.append(Message.results([ToolResult(tool_call_id="call_1", content="edited a.dart")]))
    c.append(Message.assistant(text="done"))
    return c


class Serialization(unittest.TestCase):
    def test_round_trip_is_lossless(self):
        c = _sample_conv()
        back = conv_mod.from_jsonable(conv_mod.to_jsonable(c))
        self.assertEqual(back.system_prompt, "SYS")
        self.assertEqual(len(back.messages), 4)
        # tool call preserved
        tc = back.messages[1].tool_calls[0]
        self.assertEqual((tc.id, tc.name, tc.arguments["path"]), ("call_1", "edit_file", "a.dart"))
        self.assertEqual(back.messages[1].thinking, "hmm")
        # tool result preserved
        tr = back.messages[2].tool_results[0]
        self.assertEqual((tr.tool_call_id, tr.content), ("call_1", "edited a.dart"))
        self.assertEqual(back.messages[1].role, Role.ASSISTANT)

    def test_from_jsonable_tolerates_missing_keys(self):
        c = conv_mod.from_jsonable({"messages": [{"role": "user", "text": "hi"}]})
        self.assertEqual(c.system_prompt, "")
        self.assertEqual(c.messages[0].text, "hi")


class Persistence(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        os.environ["TWOB_HISTORY_DB"] = self.db.name
        os.environ.pop("TWOB_NO_HISTORY", None)
        self.addCleanup(lambda: os.environ.pop("TWOB_HISTORY_DB", None))
        self.addCleanup(lambda: os.path.exists(self.db.name) and os.unlink(self.db.name))

    def test_save_load_round_trip(self):
        persist.save("s1", "/proj/a", "add a getter", "qwen3.5:9b", _sample_conv())
        got = persist.load("s1")
        self.assertIsNotNone(got)
        self.assertEqual(got.messages[0].text, "add a getter")
        self.assertEqual(got.messages[3].text, "done")

    def test_list_is_scoped_and_recent_first(self):
        persist.save("s1", "/proj/a", "first", "m", _sample_conv())
        persist.save("s2", "/proj/a", "second", "m", _sample_conv())
        persist.save("s3", "/proj/b", "other project", "m", _sample_conv())
        ids_a = [r["id"] for r in persist.list_sessions(cwd="/proj/a")]
        self.assertEqual(ids_a, ["s2", "s1"])                      # recent first, scoped to /proj/a
        self.assertEqual(persist.most_recent_id("/proj/a"), "s2")
        self.assertEqual([r["id"] for r in persist.list_sessions(cwd="/proj/b")], ["s3"])

    def test_save_updates_existing_and_keeps_created_at(self):
        persist.save("s1", "/proj/a", "v1", "m", _sample_conv())
        first = persist.list_sessions(cwd="/proj/a")[0]
        persist.save("s1", "/proj/a", "v2", "m", _sample_conv())
        rows = persist.list_sessions(cwd="/proj/a")
        self.assertEqual(len(rows), 1)                             # updated in place, not duplicated
        self.assertEqual(rows[0]["title"], "v2")

    def test_trivial_conversation_is_not_saved(self):
        c = Conversation(system_prompt="SYS")
        c.append(Message.user("hi"))                               # only one non-system turn
        persist.save("s1", "/proj/a", "trivial", "m", c)
        self.assertEqual(persist.list_sessions(cwd="/proj/a"), [])

    def test_disabled_is_a_noop(self):
        os.environ["TWOB_NO_HISTORY"] = "1"
        self.addCleanup(lambda: os.environ.pop("TWOB_NO_HISTORY", None))
        persist.save("s1", "/proj/a", "t", "m", _sample_conv())
        self.assertEqual(persist.list_sessions(cwd="/proj/a"), [])
        self.assertIsNone(persist.load("s1"))

    def test_load_unknown_is_none(self):
        self.assertIsNone(persist.load("nope"))

    def test_same_id_in_two_projects_does_not_clobber(self):
        # (id, cwd) composite key — the same 8-hex id in different projects coexists.
        persist.save("dup", "/proj/a", "A work", "m", _sample_conv())
        persist.save("dup", "/proj/b", "B work", "m", _sample_conv())
        self.assertEqual([r["id"] for r in persist.list_sessions(cwd="/proj/a")], ["dup"])
        self.assertEqual(persist.list_sessions(cwd="/proj/a")[0]["title"], "A work")
        self.assertEqual(persist.list_sessions(cwd="/proj/b")[0]["title"], "B work")

    def test_load_scoped_to_cwd_refuses_other_project(self):
        persist.save("s1", "/proj/a", "A", "m", _sample_conv())
        self.assertIsNotNone(persist.load("s1", cwd="/proj/a"))     # right project
        self.assertIsNone(persist.load("s1", cwd="/proj/b"))        # wrong project -> refused
        self.assertIsNotNone(persist.load("s1"))                    # unscoped still works

    def test_list_includes_message_count(self):
        persist.save("s1", "/proj/a", "t", "m", _sample_conv())   # _sample_conv has 4 messages
        self.assertEqual(persist.list_sessions(cwd="/proj/a")[0]["messages"], 4)

    def test_relative_age(self):
        now = 1_000_000.0
        self.assertEqual(persist.relative_age(now, now), "just now")
        self.assertEqual(persist.relative_age(now - 30, now), "just now")
        self.assertEqual(persist.relative_age(now - 300, now), "5m ago")
        self.assertEqual(persist.relative_age(now - 7200, now), "2h ago")
        self.assertEqual(persist.relative_age(now - 3 * 86400, now), "3d ago")
        self.assertEqual(persist.relative_age(now + 999, now), "just now")   # clock skew clamps

    def test_resume_same_id_updates_row_not_forks(self):
        # Simulates the resume path: save, then re-save under the SAME id (as the
        # resumed first task does) — one row, not two.
        persist.save("sid", "/proj/a", "do X", "m", _sample_conv())
        c2 = _sample_conv()
        c2.append(Message.user("now do Y"))
        c2.append(Message.assistant(text="did Y"))
        persist.save("sid", "/proj/a", "do X", "m", c2)
        rows = persist.list_sessions(cwd="/proj/a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(persist.load("sid", cwd="/proj/a").messages), 6)


if __name__ == "__main__":
    unittest.main()
