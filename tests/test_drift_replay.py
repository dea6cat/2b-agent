"""Tests for P10 — prompt-drift replay. A saved session records a salted hash of its
assembled prefix; `2b trace replay <id>` rebuilds the prefix with current code and reports
drift. Pure hashing + a persist round-trip against a temp DB. Run:
`python -m unittest tests.test_drift_replay`.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import driftreplay, persist  # noqa: E402
from two_b.conversation import Conversation, Message  # noqa: E402


class Hashing(unittest.TestCase):
    def test_deterministic_and_sensitive(self):
        self.assertEqual(driftreplay.prefix_hash("abc"), driftreplay.prefix_hash("abc"))
        self.assertNotEqual(driftreplay.prefix_hash("abc"), driftreplay.prefix_hash("abd"))

    def test_salted(self):
        # The hash folds in the scheme salt, so it isn't a bare sha256 of the text.
        import hashlib
        self.assertNotEqual(driftreplay.prefix_hash("abc"), hashlib.sha256(b"abc").hexdigest()[:16])


class Drift(unittest.TestCase):
    def test_identical_is_no_drift(self):
        h = driftreplay.prefix_hash("SYS")
        r = driftreplay.drift(h, "SYS")
        self.assertFalse(r["drift"])

    def test_changed_is_drift(self):
        h = driftreplay.prefix_hash("SYS v1")
        r = driftreplay.drift(h, "SYS v2")
        self.assertTrue(r["drift"])

    def test_missing_stored_hash_is_unknown(self):
        r = driftreplay.drift(None, "SYS")
        self.assertIsNone(r["drift"])
        self.assertIn("predates", r["reason"])


class ReplayRoundTrip(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        os.environ["TWOB_HISTORY_DB"] = self.db.name
        os.environ.pop("TWOB_NO_HISTORY", None)
        persist._initialized.discard(self.db.name)
        self.addCleanup(lambda: os.environ.pop("TWOB_HISTORY_DB", None))
        self.addCleanup(lambda: persist._initialized.discard(self.db.name))
        self.addCleanup(lambda: os.path.exists(self.db.name) and os.unlink(self.db.name))

    def _conv(self, system):
        c = Conversation(system_prompt=system)
        c.append(Message.user("do a thing"))
        c.append(Message.assistant(text="done"))
        return c

    def test_save_records_prefix_hash(self):
        persist.save("s1", "/proj/a", "t", "m", self._conv("SYSTEM PREFIX X"))
        meta = persist.get_meta("s1")
        self.assertEqual(meta["prefix_hash"], driftreplay.prefix_hash("SYSTEM PREFIX X"))

    def test_replay_reports_no_drift_when_prefix_unchanged(self):
        # Stub assemble_system_prompt so replay's "current" prefix matches what was saved.
        from two_b import orchestrator
        persist.save("s1", "/proj/a", "t", "m", self._conv("STABLE PREFIX"))
        orig = orchestrator.assemble_system_prompt
        orchestrator.assemble_system_prompt = lambda cwd=None: "STABLE PREFIX"
        try:
            r = driftreplay.replay("s1", cwd="/proj/a")
        finally:
            orchestrator.assemble_system_prompt = orig
        self.assertTrue(r["found"])
        self.assertFalse(r["drift"])

    def test_replay_detects_drift_when_prefix_changed(self):
        from two_b import orchestrator
        persist.save("s1", "/proj/a", "t", "m", self._conv("OLD PREFIX"))
        orig = orchestrator.assemble_system_prompt
        orchestrator.assemble_system_prompt = lambda cwd=None: "NEW PREFIX (code changed)"
        try:
            r = driftreplay.replay("s1", cwd="/proj/a")
        finally:
            orchestrator.assemble_system_prompt = orig
        self.assertTrue(r["drift"])
        self.assertIn("changed", r["reason"])

    def test_replay_unknown_session(self):
        self.assertFalse(driftreplay.replay("nope", cwd="/proj/a")["found"])

    def test_replay_scoped_to_cwd_avoids_id_collision(self):
        # Same 8-hex id in two projects: replay must resolve within the requested project.
        from two_b import orchestrator
        persist.save("dup", "/proj/a", "A", "m", self._conv("PREFIX-A"))
        persist.save("dup", "/proj/b", "B", "m", self._conv("PREFIX-B"))
        orig = orchestrator.assemble_system_prompt
        orchestrator.assemble_system_prompt = lambda cwd=None: "PREFIX-A"
        try:
            ra = driftreplay.replay("dup", cwd="/proj/a")
            rb = driftreplay.replay("dup", cwd="/proj/b")
        finally:
            orchestrator.assemble_system_prompt = orig
        self.assertFalse(ra["drift"])   # matches project A's recorded prefix
        self.assertTrue(rb["drift"])    # project B recorded a different prefix

    def test_migration_adds_column_to_legacy_db(self):
        # A pre-P10 sessions table (no prefix_hash) must gain the column and stay usable.
        conn = sqlite3.connect(self.db.name)
        conn.executescript(
            "CREATE TABLE sessions (id TEXT NOT NULL, cwd TEXT NOT NULL, title TEXT, model TEXT, "
            "created_at REAL, updated_at REAL, messages_json TEXT NOT NULL, PRIMARY KEY (id, cwd));")
        conn.commit()
        conn.close()
        persist._initialized.discard(self.db.name)     # force schema init + migration on next _db()
        persist.save("s1", "/proj/a", "t", "m", self._conv("PFX"))
        meta = persist.get_meta("s1")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["prefix_hash"], driftreplay.prefix_hash("PFX"))


if __name__ == "__main__":
    unittest.main()
