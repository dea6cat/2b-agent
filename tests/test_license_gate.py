"""First-run license acknowledgment gate: persisted once, with --yes / interactive /
non-interactive paths. Run: `python -m unittest tests.test_license_gate` from the repo root.
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

    def _out(self, s=""):
        self.out.append(str(s))

    def _fake_input(self, answer):
        self.addCleanup(setattr, builtins, "input", builtins.input)
        builtins.input = lambda *_a, **_k: answer

    def test_record_roundtrip(self):
        self.assertFalse(lic.accepted())
        lic.record()
        self.assertTrue(lic.accepted())

    def test_assume_yes_records_without_prompting(self):
        called = {"input": False}
        self.addCleanup(setattr, builtins, "input", builtins.input)
        builtins.input = lambda *_a, **_k: called.__setitem__("input", True) or "n"
        self.assertTrue(lic.ensure_accepted(assume_yes=True, interactive=True, out=self._out))
        self.assertTrue(lic.accepted())
        self.assertFalse(called["input"], "must not prompt when --yes is given")

    def test_already_accepted_is_silent(self):
        lic.record()
        self.assertTrue(lic.ensure_accepted(assume_yes=False, interactive=True, out=self._out))
        self.assertEqual(self.out, [], "no notice once already accepted")

    def test_interactive_decline_does_not_record(self):
        self._fake_input("")        # bare Enter → default No
        self.assertFalse(lic.ensure_accepted(assume_yes=False, interactive=True, out=self._out))
        self.assertFalse(lic.accepted())

    def test_interactive_accept_records(self):
        self._fake_input("y")
        self.assertTrue(lic.ensure_accepted(assume_yes=False, interactive=True, out=self._out))
        self.assertTrue(lic.accepted())

    def test_non_interactive_without_yes_blocks(self):
        self.assertFalse(lic.ensure_accepted(assume_yes=False, interactive=False, out=self._out))
        self.assertFalse(lic.accepted())

    def test_stored_id_mismatch_reprompts(self):
        config.set_pref("license_accepted", "Apache-2.0")   # a different / older id
        self.assertFalse(lic.accepted())


if __name__ == "__main__":
    unittest.main()
