"""Tests for conversation continuity — Phase 1 (cloud default, thread reset commands).

Cloud sessions carry one conversation thread across top-level messages; local sessions
stay detached (small windows). `/new` and `/clear` drop the thread. Host-side; driven over
the real run_task loop with fake providers.
Run: `python -m unittest tests.test_continuity`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import commands, orchestrator  # noqa: E402
from two_b.conversation import Conversation, Message  # noqa: E402
from two_b.providers.base import ProviderResponse  # noqa: E402
from two_b.session import Session, Task, TaskState  # noqa: E402


class _RecordingProvider:
    """Records the messages it was handed each turn; answers with 'answerN'."""
    def __init__(self, name="fake", api_key="x"):
        self.name = name
        self.api_key = api_key
        self.calls = []

    def is_available(self):
        return True

    def list_models(self):
        return ["m"]

    def stream(self, conv, model, tools, on_text):
        self.calls.append([(m.role.value, (m.text or "")) for m in conv.messages])
        n = len(self.calls)
        return ProviderResponse(message=Message.assistant(text=f"answer{n}"), raw={})


def _run(session, provider, description):
    task = Task(description=description)
    orchestrator.run_task(session, task, lambda e: None, {"fake": provider})
    return task


class CloudCarriesThread(unittest.TestCase):
    def test_second_message_sees_the_first_exchange(self):
        session = Session(default_model="fake:m")
        p = _RecordingProvider(api_key="x")           # cloud (has api_key)
        t1 = _run(session, p, "first question")
        t2 = _run(session, p, "second question")

        # The two tasks share one conversation, tracked as the session thread.
        self.assertIs(session.thread, t2.conversation)
        self.assertIs(t1.conversation, t2.conversation)

        # Turn 2's provider call carried the first Q, its persisted answer, and the new Q.
        second = " ".join(text for _role, text in p.calls[1])
        self.assertIn("first question", second)
        self.assertIn("answer1", second)              # Phase 0 persisted the final answer
        self.assertIn("second question", second)

    def test_first_call_has_no_prior_context(self):
        session = Session(default_model="fake:m")
        p = _RecordingProvider(api_key="x")
        _run(session, p, "only question")
        first = " ".join(text for _role, text in p.calls[0])
        self.assertIn("only question", first)
        self.assertEqual(first.count("answer"), 0)     # nothing carried in


class LocalStaysDetached(unittest.TestCase):
    def test_local_tasks_do_not_share_a_thread(self):
        session = Session(default_model="fake:m")
        p = _RecordingProvider(name="ollama", api_key=None)   # local
        t1 = _run(session, p, "first question")
        t2 = _run(session, p, "second question")

        self.assertIsNone(session.thread)                     # never registered
        self.assertIsNot(t1.conversation, t2.conversation)    # independent conversations
        second = " ".join(text for _role, text in p.calls[1])
        self.assertNotIn("first question", second)            # no carry-over


class OverrideRule(unittest.TestCase):
    def test_default_follows_provider(self):
        s = Session(default_model="x")
        self.assertTrue(orchestrator._continuity_effective(s, is_local=False))   # cloud on
        self.assertFalse(orchestrator._continuity_effective(s, is_local=True))   # local off

    def test_override_wins_both_ways(self):
        s = Session(default_model="x")
        s.continuity_override = True
        self.assertTrue(orchestrator._continuity_effective(s, is_local=True))    # local forced on
        s.continuity_override = False
        self.assertFalse(orchestrator._continuity_effective(s, is_local=False))  # cloud forced off


class OverrideAdoption(unittest.TestCase):
    def test_local_with_override_on_carries_thread(self):
        session = Session(default_model="fake:m")
        session.continuity_override = True
        p = _RecordingProvider(name="ollama", api_key=None)   # local
        t1 = _run(session, p, "first question")
        t2 = _run(session, p, "second question")
        self.assertIs(t1.conversation, t2.conversation)
        self.assertIsNotNone(session.thread)

    def test_cloud_with_override_off_detaches(self):
        session = Session(default_model="fake:m")
        session.continuity_override = False
        p = _RecordingProvider(api_key="x")                   # cloud
        t1 = _run(session, p, "first question")
        t2 = _run(session, p, "second question")
        self.assertIsNot(t1.conversation, t2.conversation)
        self.assertIsNone(session.thread)


class _FakeUI:
    def __init__(self): self.out = []
    def print(self, *a): self.out.append(" ".join(str(x) for x in a))


class _FakeApp:
    def __init__(self, session): self.session = session; self.ui = _FakeUI()


class ContinuityCommand(unittest.TestCase):
    def _app(self, provider):
        s = Session(default_model="fake:m")
        app = _FakeApp(s)
        app.registry = {"fake": provider}
        return s, app

    def test_on_enables_for_local_with_compaction_note(self):
        s, app = self._app(_RecordingProvider(name="ollama", api_key=None))
        commands._continuity("on", app)
        self.assertIs(s.continuity_override, True)
        self.assertTrue(any("on" in line.lower() for line in app.ui.out))
        self.assertTrue(any("compaction" in line for line in app.ui.out))

    def test_off_detaches_cloud_and_clears_thread(self):
        s, app = self._app(_RecordingProvider(api_key="x"))
        s.thread = Conversation(system_prompt="s")
        commands._continuity("off", app)
        self.assertIs(s.continuity_override, False)
        self.assertIsNone(s.thread)

    def test_bare_toggles_from_effective_state(self):
        s, app = self._app(_RecordingProvider(api_key="x"))   # cloud → effective on by default
        commands._continuity("", app)
        self.assertIs(s.continuity_override, False)            # toggled off
        commands._continuity("", app)
        self.assertIs(s.continuity_override, True)             # toggled back on

    def test_bad_arg_prints_usage_and_changes_nothing(self):
        s, app = self._app(_RecordingProvider(api_key="x"))
        commands._continuity("maybe", app)
        self.assertIsNone(s.continuity_override)
        self.assertTrue(any("Usage" in line for line in app.ui.out))


class ThreadResetCommands(unittest.TestCase):
    def _session_with_thread(self):
        s = Session(default_model="fake:m")
        s.thread = Conversation(system_prompt="s")
        s.thread.append(Message.user("earlier"))
        return s

    def test_new_drops_the_thread(self):
        s = self._session_with_thread()
        app = _FakeApp(s)
        commands._new("", app)
        self.assertIsNone(s.thread)
        self.assertIsNone(s.active_task_id)

    def test_clear_drops_the_thread(self):
        s = self._session_with_thread()
        s.tasks.append(Task(description="x"))
        app = _FakeApp(s)
        commands._clear("", app)
        self.assertIsNone(s.thread)
        self.assertEqual(s.tasks, [])

    def test_new_refuses_while_a_task_is_running(self):
        s = self._session_with_thread()
        running = Task(description="busy")
        running.state = TaskState.ACTIVE
        s.tasks.append(running)
        s.active_task_id = running.id
        app = _FakeApp(s)
        commands._new("", app)
        self.assertIsNotNone(s.thread)                        # not dropped mid-run
        self.assertTrue(any("still running" in line for line in app.ui.out))


if __name__ == "__main__":
    unittest.main()
