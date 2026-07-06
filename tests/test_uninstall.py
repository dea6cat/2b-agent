"""Tests for `2b --rm` (src/two_b/uninstall.py).

A real temp dir stands in for ~/.config/2b (real rmtree verifies deletion), while `uv`
is stubbed so no executable is actually uninstalled. Run:
`python -m unittest tests.test_uninstall` from the repo root.
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import config, uninstall  # noqa: E402


class UninstallTest(unittest.TestCase):
    def _patch(self, obj, attr, val):
        orig = getattr(obj, attr)
        setattr(obj, attr, val)
        self.addCleanup(setattr, obj, attr, orig)

    def setUp(self):
        # a real config dir with a file in it, pointed at by config.CONFIG_DIR
        self.cfg = Path(tempfile.mkdtemp()) / "2b"
        self.cfg.mkdir()
        (self.cfg / "keys.json").write_text("{}")
        self._patch(config, "CONFIG_DIR", self.cfg)
        # record uv invocations instead of running them
        self.calls = []

        def _fake_run(argv, **kw):
            self.calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        self._patch(uninstall.subprocess, "run", _fake_run)
        self._patch(uninstall.shutil, "which", lambda name: "/usr/bin/" + name)
        self._patch(uninstall, "_install_kind", lambda: "uv")    # default; overridden per-test

    def test_confirm_yes_removes_config_and_uninstalls(self):
        out = []
        code = uninstall.run(out.append, confirm=lambda p: True)
        text = "\n".join(out)
        self.assertEqual(code, 0)
        self.assertFalse(self.cfg.exists())                      # config dir really gone
        self.assertIn(["uv", "tool", "uninstall", "2b-agent"], self.calls)
        self.assertIn("Done. 2B has been removed.", text)

    def test_pip_install_uses_pip_uninstall(self):
        self._patch(uninstall, "_install_kind", lambda: "pip")
        uninstall.run([].append, confirm=lambda p: True)
        self.assertIn([sys.executable, "-m", "pip", "uninstall", "-y", "2b-agent"], self.calls)
        self.assertFalse(self.cfg.exists())

    def test_pipx_install_uses_pipx_uninstall(self):
        self._patch(uninstall, "_install_kind", lambda: "pipx")
        uninstall.run([].append, confirm=lambda p: True)
        self.assertIn(["pipx", "uninstall", "2b-agent"], self.calls)

    def test_brew_install_uses_brew_uninstall(self):
        self._patch(uninstall, "_install_kind", lambda: "brew")
        uninstall.run([].append, confirm=lambda p: True)
        self.assertIn(["brew", "uninstall", "2b-agent"], self.calls)
        self.assertFalse(self.cfg.exists())

    def test_confirm_no_aborts_and_keeps_everything(self):
        out = []
        code = uninstall.run(out.append, confirm=lambda p: False)
        self.assertEqual(code, 1)
        self.assertTrue(self.cfg.exists())                       # untouched
        self.assertEqual(self.calls, [])                         # uv never called
        self.assertIn("Aborted", "\n".join(out))

    def test_assume_yes_skips_confirm(self):
        called = {"confirm": False}

        def _confirm(p):
            called["confirm"] = True
            return False
        code = uninstall.run([].append, confirm=_confirm, assume_yes=True)
        self.assertEqual(code, 0)
        self.assertFalse(called["confirm"])                      # confirm bypassed
        self.assertFalse(self.cfg.exists())

    def test_uv_absent_notes_manual_removal(self):
        self._patch(uninstall.shutil, "which", lambda name: None)
        out = []
        uninstall.run(out.append, confirm=lambda p: True)
        text = "\n".join(out)
        self.assertEqual(self.calls, [])                         # no uv call attempted
        self.assertIn("uv not found", text)
        self.assertFalse(self.cfg.exists())                      # config still removed

    def test_lists_what_it_wont_touch(self):
        out = []
        uninstall.run(out.append, confirm=lambda p: True)
        text = "\n".join(out)
        self.assertIn("ollama rm", text)
        self.assertIn("2B.md", text)


if __name__ == "__main__":
    unittest.main()
