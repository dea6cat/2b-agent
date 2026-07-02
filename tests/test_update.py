"""Tests for the update check (src/two_b/update.py).

The cache is redirected to a temp dir and `last_check` is set to "now" so no network
call is ever made (the background refresh is throttled off). Run:
`python -m unittest tests.test_update` from the repo root.
"""
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import update  # noqa: E402


class ParseVer(unittest.TestCase):
    def test_lenient_parse(self):
        self.assertEqual(update._parse_ver("v0.2.0"), (0, 2, 0))
        self.assertEqual(update._parse_ver("1.10.3"), (1, 10, 3))
        self.assertEqual(update._parse_ver("v2.0.0-rc1"), (2, 0, 0))   # junk after digits stops
        self.assertTrue(update._parse_ver("0.3.0") > update._parse_ver("0.2.9"))


class Notice(unittest.TestCase):
    def _patch(self, obj, attr, val):
        orig = getattr(obj, attr)
        setattr(obj, attr, val)
        self.addCleanup(setattr, obj, attr, orig)

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp()) / "update_check.json"
        self._patch(update, "CACHE", self.cache)
        self._patch(update, "__version__", "0.2.0")
        self.addCleanup(os.environ.pop, "TWOB_NO_UPDATE_CHECK", None)
        self.now = time.time()

    def _cache(self, latest):
        self.cache.write_text(json.dumps({"latest": latest, "last_check": self.now}))

    def test_newer_available_notice(self):
        self._cache("v0.3.0")
        msg = update.notice(now=self.now)
        self.assertIsNotNone(msg)
        self.assertIn("0.3.0", msg)
        self.assertIn("2b --update", msg)

    def test_same_or_older_no_notice(self):
        self._cache("v0.2.0")
        self.assertIsNone(update.notice(now=self.now))
        self._cache("v0.1.9")
        self.assertIsNone(update.notice(now=self.now))

    def test_no_cache_no_notice(self):
        self.assertIsNone(update.notice(now=self.now))

    def test_opt_out(self):
        self._cache("v9.9.9")
        os.environ["TWOB_NO_UPDATE_CHECK"] = "1"
        self.assertIsNone(update.notice(now=self.now))


class RunUpgrade(unittest.TestCase):
    def _patch(self, obj, attr, val):
        orig = getattr(obj, attr)
        setattr(obj, attr, val)
        self.addCleanup(setattr, obj, attr, orig)

    def test_uv_absent_returns_1(self):
        self._patch(update.shutil, "which", lambda n: None)
        out = []
        code = update.run_upgrade(out.append)
        self.assertEqual(code, 1)
        self.assertIn("uv not found", "\n".join(out))

    def test_uv_present_invokes_upgrade(self):
        self._patch(update.shutil, "which", lambda n: "/usr/bin/uv")
        calls = []
        self._patch(update.subprocess, "run",
                    lambda argv, **kw: calls.append(argv) or types.SimpleNamespace(returncode=0))
        code = update.run_upgrade([].append)
        self.assertEqual(code, 0)
        self.assertIn(["uv", "tool", "upgrade", "2b-agent"], calls)


if __name__ == "__main__":
    unittest.main()
