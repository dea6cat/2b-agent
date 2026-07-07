"""Tests for the P2 never-throw turn closure.

run_task must always end with a clean terminal event and a non-empty final
message — never an exception escaping the worker thread (which would hang the
UI on no event), a bare trace, or empty output. These drive run_task with fake
providers over the real loop. Pure host-side — no model needed.
Run: `python -m unittest tests.test_turn_closure`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402
from two_b.conversation import Message, Role, ToolCall  # noqa: E402
from two_b.orchestrator import EventType, _classify_exc, _finish_failed  # noqa: E402
from two_b.providers.base import ProviderError, ProviderResponse  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class _FakeProvider:
    """A cloud-shaped provider (has an api_key, so run_task takes the cloud path)."""
    name = "fake"
    api_key = "x"

    def is_available(self):
        return True

    def list_models(self):
        return ["m"]


class _Raises(_FakeProvider):
    def __init__(self, exc):
        self._exc = exc

    def stream(self, conv, model, tools, on_text):
        raise self._exc


class _FinalText(_FakeProvider):
    def stream(self, conv, model, tools, on_text):
        on_text("all done")
        return ProviderResponse(message=Message.assistant(text="all done"), raw={})


class _EmptyLength(_FakeProvider):
    def stream(self, conv, model, tools, on_text):
        return ProviderResponse(message=Message.assistant(), raw={}, done_reason="length")


class _CallsTool(_FakeProvider):
    """Returns one tool call so dispatch runs — the path where a raising
    _dispatch_tool previously escaped run_task entirely (past only `finally`)."""
    def stream(self, conv, model, tools, on_text):
        call = ToolCall.new("read_file", {"path": "a.py"})
        return ProviderResponse(message=Message.assistant(tool_calls=[call]), raw={})


def _run(provider):
    """Drive run_task once with `provider` and return the list of emitted events."""
    session = Session(default_model="fake:m")
    task = Task(description="do a thing")
    events = []
    orchestrator.run_task(session, task, events.append, {"fake": provider})
    return task, events


def _types(events):
    return [e.type for e in events]


class NeverThrows(unittest.TestCase):
    def test_provider_exception_becomes_task_error_not_a_raise(self):
        # A generic (non-Provider) exception must not escape the worker thread.
        task, events = _run(_Raises(RuntimeError("boom")))
        self.assertIn(EventType.TASK_ERROR, _types(events))
        self.assertNotIn(EventType.TASK_DONE, _types(events))
        err = next(e for e in events if e.type == EventType.TASK_ERROR)
        self.assertTrue(err.payload["error"])                     # non-empty
        self.assertIn("boom", err.payload["error"])
        self.assertEqual(task.error, err.payload["error"])

    def test_blank_message_exception_still_names_its_type(self):
        # KeyError('') → str(e) is nearly empty; the reason must never be blank.
        task, events = _run(_Raises(KeyError()))
        err = next(e for e in events if e.type == EventType.TASK_ERROR)
        self.assertTrue(err.payload["error"].strip())
        self.assertIn("KeyError", err.payload["error"])

    def test_provider_error_reason_is_preserved(self):
        task, events = _run(_Raises(ProviderError("fake", "rate limited")))
        err = next(e for e in events if e.type == EventType.TASK_ERROR)
        self.assertIn("rate limited", err.payload["error"])

    def test_exception_from_tool_dispatch_is_caught_by_outer_net(self):
        # The headline P2 bug: a non-cancel exception raised inside _dispatch_tool
        # re-raises and, before this phase, escaped run_task past only `finally`,
        # killing the worker thread with no terminal event (UI hangs). The new outer
        # except must turn it into exactly one clean TASK_ERROR.
        orig = orchestrator._dispatch_tool
        orchestrator._dispatch_tool = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dispatch boom"))
        try:
            task, events = _run(_CallsTool())
        finally:
            orchestrator._dispatch_tool = orig
        types = _types(events)
        self.assertEqual(types.count(EventType.TASK_ERROR), 1)
        self.assertNotIn(EventType.TASK_DONE, types)
        err = next(e for e in events if e.type == EventType.TASK_ERROR)
        self.assertIn("dispatch boom", err.payload["error"])

    def test_normal_completion_emits_final_message_then_done(self):
        task, events = _run(_FinalText())
        self.assertIn(EventType.TASK_DONE, _types(events))
        self.assertNotIn(EventType.TASK_ERROR, _types(events))
        deltas = [e.payload["chunk"] for e in events if e.type == EventType.ASSISTANT_DELTA]
        self.assertEqual("".join(deltas), "all done")

    def test_empty_truncated_answer_is_surfaced_not_silent(self):
        # No content, no tool call, done_reason=length → a named final message.
        task, events = _run(_EmptyLength())
        self.assertIn(EventType.TASK_DONE, _types(events))
        deltas = [e.payload["chunk"] for e in events if e.type == EventType.ASSISTANT_DELTA]
        self.assertEqual(len(deltas), 1)
        self.assertIn("cut off", deltas[0])


class PersistsFinalAnswer(unittest.TestCase):
    """Phase 0 of continuity: the closing assistant answer must land in the conversation
    so a thread carried forward (continuity / steer re-attach) actually contains it."""

    def test_final_answer_is_appended_to_conversation(self):
        task, _ = _run(_FinalText())
        msgs = task.conversation.messages
        self.assertEqual(msgs[-1].role, Role.ASSISTANT)
        self.assertEqual(msgs[-1].text, "all done")

    def test_empty_answer_is_not_appended(self):
        # A length-truncated / empty final turn must not pollute history with a blank turn.
        task, _ = _run(_EmptyLength())
        self.assertFalse(any(m.role == Role.ASSISTANT for m in task.conversation.messages))

    def test_thinking_only_answer_is_persisted_as_text(self):
        # A reasoning model may put its answer in `thinking` with empty text; history
        # should carry what the UI showed (the thinking fallback) as a clean text turn.
        class _ThinkingOnly(_FakeProvider):
            def stream(self, conv, model, tools, on_text):
                return ProviderResponse(message=Message.assistant(thinking="the answer is 42"), raw={})

        task, _ = _run(_ThinkingOnly())
        msgs = task.conversation.messages
        self.assertEqual(msgs[-1].role, Role.ASSISTANT)
        self.assertEqual(msgs[-1].text, "the answer is 42")

    def test_answer_after_a_tool_call_is_the_last_message(self):
        # Turn 1 calls a tool, turn 2 answers; the answer (not the tool results) must be
        # the last thing in the thread, so the next message continues from it.
        class _ToolThenAnswer(_FakeProvider):
            def __init__(self): self.i = 0
            def stream(self, conv, model, tools, on_text):
                self.i += 1
                if self.i == 1:
                    return ProviderResponse(message=Message.assistant(
                        tool_calls=[ToolCall.new("list_files", {"path": "."})]), raw={})
                on_text("here is the summary")
                return ProviderResponse(message=Message.assistant(text="here is the summary"), raw={})

        task, _ = _run(_ToolThenAnswer())
        msgs = task.conversation.messages
        self.assertEqual(msgs[-1].role, Role.ASSISTANT)
        self.assertEqual(msgs[-1].text, "here is the summary")


class Classify(unittest.TestCase):
    def test_classify_provider_error_keeps_message(self):
        self.assertIn("nope", _classify_exc(ProviderError("p", "nope")))

    def test_classify_blank_exception_uses_type_name(self):
        self.assertEqual(_classify_exc(ValueError()), "ValueError")

    def test_classify_exception_with_message(self):
        self.assertEqual(_classify_exc(ValueError("bad")), "ValueError: bad")

    def test_finish_failed_never_emits_blank_reason(self):
        task = Task(description="t")
        events = []
        _finish_failed(task, events.append, "   ")
        self.assertEqual(task.error, "unknown error")
        self.assertEqual(events[0].payload["error"], "unknown error")


if __name__ == "__main__":
    unittest.main()
