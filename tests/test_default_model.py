"""Tests for the /default command, its prefs persistence, and local/cloud classification.

Config paths are redirected to a temp dir so the real ~/.config/2b is never touched.
Run: `python -m unittest tests.test_default_model` from the repo root.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import commands, config, registry  # noqa: E402


class _FakeProvider:
    def __init__(self, name, models, api_key=None):
        self.name = name
        self._models = models
        self.api_key = api_key

    def is_available(self):
        return True

    def list_models(self):
        return self._models


class _FakeUI:
    def __init__(self):
        self.out = []

    def print(self, *a):
        self.out.append(" ".join(str(x) for x in a))


class _FakeSession:
    def __init__(self):
        self.default_model = ""
        self.active_task = None


class _FakeApp:
    def __init__(self, reg):
        self.registry = reg
        self.session = _FakeSession()
        self.ui = _FakeUI()


class ConfigDirTest(unittest.TestCase):
    """Redirect config's file paths at module level so prefs go to a temp dir."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig_cfgdir, self._orig_prefs = config.CONFIG_DIR, config.PREFS_FILE
        config.CONFIG_DIR = Path(self._dir)
        config.PREFS_FILE = Path(self._dir) / "prefs.json"

    def tearDown(self):
        config.CONFIG_DIR, config.PREFS_FILE = self._orig_cfgdir, self._orig_prefs


class Prefs(ConfigDirTest):
    def test_roundtrip_and_delete(self):
        self.assertEqual(config.get_prefs(), {})
        config.set_pref("default_model", "ollama:qwen3.5:9b")
        self.assertEqual(config.get_prefs()["default_model"], "ollama:qwen3.5:9b")
        config.set_pref("default_model", None)
        self.assertNotIn("default_model", config.get_prefs())


class IsLocal(unittest.TestCase):
    def test_classification(self):
        self.assertTrue(registry.is_local(_FakeProvider("ollama", [])))
        self.assertFalse(registry.is_local(_FakeProvider("ollama", [], api_key="k")))   # cloud-ollama
        self.assertFalse(registry.is_local(_FakeProvider("anthropic", [], api_key="k")))


class DefaultCommand(ConfigDirTest):
    def test_set_switches_and_persists_local(self):
        app = _FakeApp({"ollama": _FakeProvider("ollama", ["qwen3.5:9b"])})
        commands._default("ollama:qwen3.5:9b", app)
        self.assertEqual(app.session.default_model, "ollama:qwen3.5:9b")
        self.assertEqual(config.get_prefs()["default_model"], "ollama:qwen3.5:9b")
        self.assertTrue(any("(local)" in l for l in app.ui.out))

    def test_set_cloud_labelled_cloud(self):
        app = _FakeApp({"anthropic": _FakeProvider("anthropic", ["claude-sonnet-5"], api_key="k")})
        commands._default("anthropic:claude-sonnet-5", app)
        self.assertEqual(config.get_prefs()["default_model"], "anthropic:claude-sonnet-5")
        self.assertTrue(any("(cloud)" in l for l in app.ui.out))

    def test_bare_shows_saved_default(self):
        config.set_pref("default_model", "ollama:qwen3.5:9b")
        app = _FakeApp({"ollama": _FakeProvider("ollama", ["qwen3.5:9b"])})
        commands._default("", app)
        self.assertTrue(any("Default model" in l and "(local)" in l for l in app.ui.out))

    def test_unresolvable_errors_and_does_not_persist(self):
        app = _FakeApp({"ollama": _FakeProvider("ollama", ["qwen3.5:9b"])})
        commands._default("does-not-exist", app)
        self.assertTrue(any("Could not resolve" in l for l in app.ui.out))
        self.assertNotIn("default_model", config.get_prefs())


if __name__ == "__main__":
    unittest.main()
