"""Tests for `2b --doctor` (src/two_b/doctor.py).

doctor.run is driven with a capturing emitter and patched deps (no real uv/Ollama/network),
so every branch — healthy, PATH-missing, Ollama-down, no-default — is exercised deterministically.
Run: `python -m unittest tests.test_doctor` from the repo root.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import doctor, registry as R  # noqa: E402


class _FakeProvider:
    def __init__(self, name, models, api_key=None, avail=True, raises=False):
        self.name = name
        self._models = models
        self.api_key = api_key
        self._avail = avail
        self._raises = raises

    def is_available(self):
        return self._avail

    def list_models(self):
        if self._raises:
            raise OSError("connection refused")
        return self._models


class DoctorTest(unittest.TestCase):
    def _patch(self, obj, attr, val):
        orig = getattr(obj, attr)
        setattr(obj, attr, val)
        self.addCleanup(setattr, obj, attr, orig)

    def _run(self, *, which, bindir, path, reg, prefs, pick):
        self._patch(doctor, "shutil", types.SimpleNamespace(which=lambda name: which))
        self._patch(doctor, "_bin_dir", lambda: bindir)
        self._patch(doctor, "registry", types.SimpleNamespace(
            build_registry=lambda: reg, usable=R.usable, resolve=R.resolve, is_local=R.is_local))
        self._patch(doctor, "config", types.SimpleNamespace(get_prefs=lambda: prefs))

        def _pick():
            if isinstance(pick, BaseException):
                raise pick
            return pick
        self._patch(doctor, "orchestrator", types.SimpleNamespace(pick_default_model=_pick))
        self._patch(os, "environ", dict(os.environ, PATH=path))

        out = []
        code = doctor.run(out.append)
        return code, "\n".join(out)

    def test_healthy(self):
        code, text = self._run(
            which="/Users/x/.local/bin/2b", bindir="/Users/x/.local/bin",
            path="/Users/x/.local/bin:/usr/bin",
            reg={"ollama": _FakeProvider("ollama", ["qwen3.5:9b"])},
            prefs={}, pick="qwen3.5:9b")
        self.assertEqual(code, 0)
        self.assertIn("on PATH:", text)
        self.assertIn("Ollama reachable — 1 local model(s)", text)
        self.assertIn("default model: qwen3.5:9b (local)", text)
        self.assertIn("All checks passed", text)

    def test_path_missing_prints_fix(self):
        code, text = self._run(
            which=None, bindir="/opt/uvbin", path="/usr/bin:/bin",
            reg={"ollama": _FakeProvider("ollama", ["qwen3.5:9b"])},
            prefs={}, pick="qwen3.5:9b")
        self.assertEqual(code, 1)
        self.assertIn("not on your PATH", text)
        self.assertIn("uv tool update-shell", text)
        self.assertIn("/opt/uvbin", text)

    def test_ollama_down_and_no_default(self):
        code, text = self._run(
            which="/x/2b", bindir="/x", path="/x",
            reg={"ollama": _FakeProvider("ollama", [], avail=False, raises=True)},
            prefs={}, pick=SystemExit("No local Ollama models found."))
        self.assertEqual(code, 1)
        self.assertIn("Ollama not reachable", text)
        self.assertIn("no default model available", text)
        self.assertIn("providers configured: none", text)

    def test_cloud_default_labelled(self):
        code, text = self._run(
            which="/x/2b", bindir="/x", path="/x",
            reg={"anthropic": _FakeProvider("anthropic", ["claude-sonnet-5"], api_key="k")},
            prefs={"default_model": "anthropic:claude-sonnet-5"}, pick="unused")
        self.assertEqual(code, 0)
        self.assertIn("default model: anthropic:claude-sonnet-5 (cloud)  [saved default]", text)


if __name__ == "__main__":
    unittest.main()
