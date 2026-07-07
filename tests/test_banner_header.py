"""Tests for the banner header reflecting the *current* model/provider.

Regression: after `/model` to a cloud model the header kept showing the startup model and a
hardcoded "local · Ollama". The header must refresh for cloud too, and show local/cloud +
the real provider. TwoBApp pulls in textual (runtime-only dep); skip when it's absent.
Run: `python -m unittest tests.test_banner_header`.
"""
import asyncio
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402

try:
    from two_b.app_tui import TwoBApp, _provider_display  # noqa: E402
    _HAS_TEXTUAL = True
except ModuleNotFoundError:
    TwoBApp = None
    _HAS_TEXTUAL = False


class _FakeProvider:
    def __init__(self, name, api_key):
        self.name = name
        self.api_key = api_key

    def is_available(self):
        return True

    def list_models(self):
        return ["big-model"]


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed (runtime-only dependency)")
class ProviderDisplay(unittest.TestCase):
    def test_known_names_get_nice_labels(self):
        self.assertEqual(_provider_display("google"), "Google")
        self.assertEqual(_provider_display("nvidia"), "NVIDIA")
        self.assertEqual(_provider_display("openai"), "OpenAI")
        self.assertEqual(_provider_display("ollama"), "Ollama")

    def test_unknown_name_falls_back_to_capitalized(self):
        self.assertEqual(_provider_display("mistral"), "Mistral")

    def test_empty(self):
        self.assertEqual(_provider_display(""), "")


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed (runtime-only dependency)")
class HeaderRefresh(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_budget = orchestrator.context_budget

    def tearDown(self):
        orchestrator.context_budget = self._orig_budget

    async def _refresh_and_read(self, app, model, provider):
        """Point the session at `model`/`provider` and run the async loader as the app
        does (in a thread), then read the rendered header."""
        from textual.widgets import Static
        app.registry = {provider.name: provider}
        app.session.default_model = model
        # Run the loader as the app does (a daemon thread). It calls call_from_thread, which
        # needs THIS event loop free to run — so await (never join), letting the loop both
        # unblock the thread and process the queued header update.
        t = threading.Thread(target=app._load_ctx_label)
        t.start()
        for _ in range(200):
            if not t.is_alive():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        return str(app.query_one("#header", Static).render())

    async def test_cloud_switch_updates_header(self):
        orchestrator.context_budget = lambda p, m: 1_000_000
        app = TwoBApp(model="fake:m", auto_yes=True, initial_task=None)
        async with app.run_test():
            text = await self._refresh_and_read(
                app, "nvidia:deepseek-v4-flash", _FakeProvider("nvidia", api_key="k"))
            self.assertIn("nvidia:deepseek-v4-flash", text)   # current model, not the startup one
            self.assertIn("cloud", text)
            self.assertIn("NVIDIA", text)
            self.assertNotIn("local  ·  Ollama", text)         # no longer hardcoded

    async def test_local_still_shows_window_and_ollama(self):
        orchestrator.context_budget = lambda p, m: 13000
        app = TwoBApp(model="fake:m", auto_yes=True, initial_task=None)
        async with app.run_test():
            text = await self._refresh_and_read(
                app, "ollama:qwen3.5:9b", _FakeProvider("ollama", api_key=None))
            self.assertIn("qwen3.5:9b", text)
            self.assertIn("local", text)
            self.assertIn("Ollama", text)
            self.assertIn("13k ctx", text)


if __name__ == "__main__":
    unittest.main()
