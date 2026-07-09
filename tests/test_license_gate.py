"""First-run license acknowledgment gate: validated every run, persisted once, with
--yes / accept / explicit-decline (uninstall) / cancel / non-interactive paths.
Run: `python -m unittest tests.test_license_gate` from the repo root.
"""
import builtins
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import config  # noqa: E402
from two_b import license as lic  # noqa: E402


class Gate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        # Point config at a throwaway prefs dir so tests never touch the real ~/.config/2b.
        self.addCleanup(setattr, config, "CONFIG_DIR", config.CONFIG_DIR)
        self.addCleanup(setattr, config, "PREFS_FILE", config.PREFS_FILE)
        config.CONFIG_DIR = Path(self.dir)
        config.PREFS_FILE = Path(self.dir) / "prefs.json"
        self.out = []
        self.declined = {"n": 0}

    def _out(self, s=""):
        self.out.append(str(s))

    def _on_decline(self):
        self.declined["n"] += 1

    def _fake_input(self, answer):
        self.addCleanup(setattr, builtins, "input", builtins.input)
        builtins.input = lambda *_a, **_k: answer

    def _ensure(self, **kw):
        kw.setdefault("out", self._out)
        kw.setdefault("on_decline", self._on_decline)
        return lic.ensure_accepted(**kw)

    def test_record_roundtrip(self):
        self.assertFalse(lic.accepted())
        lic.record()
        self.assertTrue(lic.accepted())

    def test_assume_yes_records_without_prompting(self):
        called = {"input": False}
        self.addCleanup(setattr, builtins, "input", builtins.input)
        builtins.input = lambda *_a, **_k: called.__setitem__("input", True) or "n"
        self.assertTrue(self._ensure(assume_yes=True, interactive=True))
        self.assertTrue(lic.accepted())
        self.assertFalse(called["input"], "must not prompt when --yes is given")
        self.assertEqual(self.declined["n"], 0)

    def test_already_accepted_is_silent(self):
        lic.record()
        self.assertTrue(self._ensure(assume_yes=False, interactive=True))
        self.assertEqual(self.out, [], "no notice once already accepted")

    def test_interactive_accept_records(self):
        self._fake_input("y")
        self.assertTrue(self._ensure(assume_yes=False, interactive=True))
        self.assertTrue(lic.accepted())
        self.assertEqual(self.declined["n"], 0)

    def test_explicit_no_declines_and_uninstalls(self):
        self._fake_input("n")
        self.assertFalse(self._ensure(assume_yes=False, interactive=True))
        self.assertFalse(lic.accepted(), "decline must not record acceptance")
        self.assertEqual(self.declined["n"], 1, "explicit 'n' must trigger the uninstall hook")

    def test_enter_cancels_without_uninstalling(self):
        self._fake_input("")        # bare Enter = cancel
        self.assertFalse(self._ensure(assume_yes=False, interactive=True))
        self.assertFalse(lic.accepted())
        self.assertEqual(self.declined["n"], 0, "Enter must NOT uninstall")

    def test_unrecognized_answer_cancels_without_uninstalling(self):
        self._fake_input("maybe")   # anything that isn't y/n = cancel, not decline
        self.assertFalse(self._ensure(assume_yes=False, interactive=True))
        self.assertEqual(self.declined["n"], 0)

    def test_non_interactive_without_yes_blocks_without_uninstalling(self):
        self.assertFalse(self._ensure(assume_yes=False, interactive=False))
        self.assertFalse(lic.accepted())
        self.assertEqual(self.declined["n"], 0, "a non-interactive run must never uninstall")

    def test_stored_id_mismatch_reprompts(self):
        config.set_pref("license_accepted", "Apache-2.0")   # a different / older id
        self.assertFalse(lic.accepted())


if __name__ == "__main__":
    unittest.main()
