"""Full-screen (Textual) live-thinking render: a THINKING_DELTA streams into a dim `.thinking`
region that collapses to a `💭 thought for Ns` summary once the reply (ASSISTANT_DELTA) starts.
Guarded on textual (runtime-only dep). Run: `python -m unittest tests.test_thinking_tui`.
"""
import os
import sys
import unittest

os.environ.setdefault("TEXTUAL_ANIMATIONS", "none")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from textual.containers import VerticalScroll  # noqa: F401
    from two_b.app_tui import TwoBApp
    from two_b.orchestrator import AgentEvent, EventType
    _HAS_TEXTUAL = True
except ModuleNotFoundError:
    _HAS_TEXTUAL = False


def _render(widget) -> str:
    # Textual 8.x Static has no .renderable; render() gives the current content.
    try:
        return str(widget.render())
    except Exception:
        return str(getattr(widget, "_content", ""))


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed (runtime-only dependency)")
class ThinkingTui(unittest.IsolatedAsyncioTestCase):
    async def test_thinking_streams_then_collapses_on_reply(self):
        app = TwoBApp(model="fake:m", auto_yes=True, initial_task=None)
        async with app.run_test() as pilot:
            await pilot.pause()

            # 1) thinking streams into a live dim region
            app.session.events.put(AgentEvent(EventType.THINKING_DELTA, "t", {"chunk": "weighing "}))
            app.session.events.put(AgentEvent(EventType.THINKING_DELTA, "t", {"chunk": "the options"}))
            app._drain_events()
            await pilot.pause()
            thinking = app.query(".thinking")
            self.assertTrue(len(thinking) >= 1)
            self.assertIn("weighing the options", _render(thinking.first()))

            # 2) the reply starts -> thinking collapses to a summary, reply renders
            app.session.events.put(AgentEvent(EventType.ASSISTANT_DELTA, "t", {"chunk": "Here is the fix"}))
            app._drain_events()
            await pilot.pause()
            self.assertIn("thought for", _render(app.query(".thinking").first()))
            self.assertIn("Here is the fix", _render(app.query(".reply").first()))

    async def test_thinking_only_turn_stays_expanded(self):
        # A turn that produced only thinking (no reply) leaves the reasoning visible as the output.
        app = TwoBApp(model="fake:m", auto_yes=True, initial_task=None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.session.events.put(AgentEvent(EventType.THINKING_DELTA, "t", {"chunk": "just musing"}))
            app._drain_events()
            await pilot.pause()
            app.session.events.put(AgentEvent(EventType.TASK_DONE, "t", {}))
            app._drain_events()
            await pilot.pause()
            # left expanded (not collapsed to "thought for"): the streamed reasoning stands
            self.assertIn("just musing", _render(app.query(".thinking").first()))

    async def test_turn_boundary_collapses_thinking_no_merge(self):
        # think -> (turn/tool boundary) -> think again must NOT merge into one widget: the first
        # collapses to a summary, the second is a fresh region (order preserved, no stale leak).
        app = TwoBApp(model="fake:m", auto_yes=True, initial_task=None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.session.events.put(AgentEvent(EventType.THINKING_DELTA, "t", {"chunk": "round one"}))
            app.session.events.put(AgentEvent(EventType.TURN_START, "t", {}))
            app.session.events.put(AgentEvent(EventType.THINKING_DELTA, "t", {"chunk": "round two"}))
            app._drain_events()
            await pilot.pause()
            regions = [_render(w) for w in app.query(".thinking")]
            self.assertTrue(any("thought for" in r for r in regions))   # round one collapsed
            self.assertTrue(any("round two" in r and "round one" not in r for r in regions))  # fresh, not merged

    async def test_error_does_not_bleed_thinking_into_next_task(self):
        # A live thinking region at TASK_ERROR must be committed, so the next task's thinking
        # starts fresh instead of appending onto the errored task's stale widget.
        app = TwoBApp(model="fake:m", auto_yes=True, initial_task=None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.session.events.put(AgentEvent(EventType.THINKING_DELTA, "t1", {"chunk": "task one thoughts"}))
            app.session.events.put(AgentEvent(EventType.TASK_ERROR, "t1", {"error": "boom"}))
            app.session.events.put(AgentEvent(EventType.THINKING_DELTA, "t2", {"chunk": "task two thoughts"}))
            app._drain_events()
            await pilot.pause()
            regions = [_render(w) for w in app.query(".thinking")]
            self.assertTrue(any("task two thoughts" in r and "task one thoughts" not in r for r in regions))


if __name__ == "__main__":
    unittest.main()
