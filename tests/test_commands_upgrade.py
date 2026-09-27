import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from two_b.commands import dispatch_input, command_specs, _context, _compact, _ctx


class CommandRegistration(unittest.TestCase):
    def test_compact_registered(self):
        names = [s[0] for s in command_specs()]
        self.assertIn("compact", names)

    def test_context_registered(self):
        names = [s[0] for s in command_specs()]
        self.assertIn("context", names)

    def test_skills_registered(self):
        names = [s[0] for s in command_specs()]
        self.assertIn("skills", names)


class Dispatch(unittest.TestCase):
    class _FakeApp:
        def __init__(self):
            self.prints = []
            self.cwd = "/tmp"
            self.session = type("S", (), {"tasks": [], "active_task": None,
                                          "active_task_id": None, "default_model": ""})()
            self.registry = {}
        def print(self, msg="", **kw):
            self.prints.append(str(msg))
        ui = property(lambda self: self)

    def test_unknown_command_shows_error(self):
        app = self._FakeApp()
        result = dispatch_input("/nonexistent", app)
        self.assertTrue(result)
        self.assertTrue(any("Unknown command" in p for p in app.prints))

    def test_non_slash_returns_false(self):
        app = self._FakeApp()
        result = dispatch_input("hello world", app)
        self.assertFalse(result)


class CompactCommand(unittest.TestCase):
    class _FakeApp:
        def __init__(self):
            self.prints = []
            self.cwd = "/tmp"
        def print(self, msg="", **kw):
            self.prints.append(str(msg))
        ui = property(lambda self: self)

    def test_compact_no_task(self):
        app = self._FakeApp()
        app.session = type("S", (), {"tasks": [], "active_task": None,
                                      "active_task_id": None, "default_model": ""})()
        app.registry = {}
        _compact("", app)
        self.assertTrue(any("No conversation" in p for p in app.prints))


class ContextCommand(unittest.TestCase):
    class _FakeApp:
        def __init__(self):
            self.prints = []
            self.cwd = "/tmp"
        def print(self, msg="", **kw):
            self.prints.append(str(msg))
        ui = property(lambda self: self)

    def test_context_no_task(self):
        app = self._FakeApp()
        app.session = type("S", (), {"tasks": [], "active_task": None,
                                    "active_task_id": None, "default_model": ""})()
        app.registry = {}
        _context("", app)
        self.assertTrue(any("No conversation" in p for p in app.prints))


class CtxCommand(unittest.TestCase):
    class _FakeProvider:
        name = "ollama"
        def __init__(self, api_key=None):
            self.api_key = api_key
            self._ctx_override = {}
        def is_available(self):
            return True
        def context_window(self, model):
            return self._ctx_override.get(model, 8192)
        def _compute_ctx(self, model):
            return 4096
        def set_context_window(self, model, tokens):
            if tokens is None or tokens <= 0:
                self._ctx_override.pop(model, None)
            else:
                self._ctx_override[model] = tokens

    def _make_app(self, provider=None, model_name="gemma3:12b-mlx"):
        if provider is None:
            provider = self._FakeProvider()
        app = type("FakeApp", (), {})()
        app.prints = []
        app.cwd = "/tmp"
        app.on_context_changed = None
        class _UI:
            def __init__(self_outer):
                self_outer._prints = []
            def print(self_outer, msg="", **kw):
                self._prints.append(str(msg))
        ui = _UI()
        ui._prints = app.prints
        def _print(msg="", **kw):
            app.prints.append(str(msg))
        ui.print = _print
        app.ui = ui
        task = type("T", (), {"model_override": None})()
        app.session = type("S", (), {
            "default_model": f"ollama:{model_name}",
            "active_task": task,
            "tasks": [task],
            "active_task_id": "t1",
        })()
        app.registry = {"ollama": provider}
        return app

    def test_ctx_show_no_active_task(self):
        app = self._make_app()
        app.session.active_task = None
        app.session.tasks = []
        _ctx("", app)
        self.assertTrue(any("No active task" in p for p in app.prints))

    def test_ctx_set_k_suffix(self):
        app = self._make_app()
        _ctx("32k", app)
        provider = app.registry["ollama"]
        self.assertEqual(provider._ctx_override.get("gemma3:12b-mlx"), 32768)

    def test_ctx_set_exact_number(self):
        app = self._make_app()
        _ctx("65536", app)
        provider = app.registry["ollama"]
        self.assertEqual(provider._ctx_override.get("gemma3:12b-mlx"), 65536)

    def test_ctx_auto_resets_override(self):
        app = self._make_app()
        _ctx("32k", app)
        _ctx("auto", app)
        provider = app.registry["ollama"]
        self.assertNotIn("gemma3:12b-mlx", provider._ctx_override)

    def test_ctx_rejects_cloud_provider(self):
        provider = self._FakeProvider(api_key="key")
        app = self._make_app(provider=provider)
        _ctx("32k", app)
        self.assertTrue(any("cloud" in p.lower() or "only applies" in p for p in app.prints))

    def test_ctx_rejects_invalid_syntax(self):
        app = self._make_app()
        _ctx("abcdef", app)
        self.assertTrue(any("Usage" in p for p in app.prints))
        provider = app.registry["ollama"]
        self.assertNotIn("gemma3:12b-mlx", provider._ctx_override)

    def test_ctx_registered_in_command_specs(self):
        names = [s[0] for s in command_specs()]
        self.assertIn("ctx", names)


if __name__ == "__main__":
    unittest.main()
