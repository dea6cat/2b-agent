"""run_task emits THINKING_DELTA for streamed thinking, gated by TWOB_NO_THINK_DISPLAY.
Run: `python -m unittest tests.test_thinking_orchestration`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator as O  # noqa: E402
from two_b.conversation import Message  # noqa: E402
from two_b.providers.base import ProviderResponse  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class _ThinkingProvider:
    """A thinking model that emits one reasoning chunk then a reply, no tool calls."""
    name, api_key = "fake", "x"

    def is_available(self):
        return True

    def list_models(self):
        return ["m"]

    def supports_reasoning(self, model):
        return True

    def stream(self, conv, model, tools, on_text, *, cancel=None, reasoning=None, on_thinking=None):
        if on_thinking:
            on_thinking("weighing options")
        on_text("done")
        return ProviderResponse(message=Message.assistant(text="done", thinking="weighing options"), raw={})


def _run_events(env=None):
    for k, v in (env or {}).items():
        os.environ[k] = v
    s = Session(default_model="fake:m")
    t = Task(description="do x")
    events = []
    O.run_task(s, t, events.append, {"fake": _ThinkingProvider()})   # (session, task, on_event, registry)
    return events


class ThinkingOrchestration(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("TWOB_NO_THINK_DISPLAY", None)

    def test_thinking_delta_emitted(self):
        evs = _run_events()
        chunks = [e.payload.get("chunk") for e in evs if e.type == O.EventType.THINKING_DELTA]
        self.assertIn("weighing options", chunks)

    def test_suppressed_when_display_off(self):
        evs = _run_events({"TWOB_NO_THINK_DISPLAY": "1"})
        self.assertFalse([e for e in evs if e.type == O.EventType.THINKING_DELTA])


if __name__ == "__main__":
    unittest.main()
