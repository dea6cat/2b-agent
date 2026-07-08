"""Tests for the clarification-instead-of-acting nudge.

A small model faced with an actionable request sometimes returns a wall of clarifying
questions instead of investigating. `_asked_instead_of_acting` detects that shape so the
turn loop can nudge it to look with the read-only tools first. Host-side only.
Run: `python -m unittest tests.test_clarify_nudge`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator as O  # noqa: E402
from two_b import orchestrator  # noqa: E402
from two_b.conversation import Message  # noqa: E402
from two_b.providers.base import ProviderResponse  # noqa: E402
from two_b.session import Session, Task  # noqa: E402


class ClarifyDetectTest(unittest.TestCase):
    def test_asks_which_files_and_what_to_change_is_flagged(self):
        # The exact shape observed on qwen3.5 / gemma: punt with numbered questions.
        self.assertTrue(O._asked_instead_of_acting(
            "I'm happy to help, but I need a bit more detail to know exactly what you'd like "
            "me to change. Could you please specify: 1) Which file(s) or component(s) you'd "
            "like to modify? 2) What the change should accomplish?"))

    def test_short_clarification_requests_flagged(self):
        self.assertTrue(O._asked_instead_of_acting("Could you clarify which module you mean?"))
        self.assertTrue(O._asked_instead_of_acting("Please specify what the function should return?"))
        self.assertTrue(O._asked_instead_of_acting("I need more context. What do you want it to do?"))

    def test_spanish_clarification_flagged(self):
        self.assertTrue(O._asked_instead_of_acting("Necesito más detalles. ¿Podrías especificar el archivo?"))

    def test_plain_answer_not_flagged(self):
        self.assertFalse(O._asked_instead_of_acting(
            "The package is a Dart agent framework. It exports three classes."))

    def test_courtesy_offer_not_flagged(self):
        # A done-report that ends with a polite offer is a real answer, not a punt.
        self.assertFalse(O._asked_instead_of_acting("I fixed the bug in foo.py. Want me to add tests too?"))
        self.assertFalse(O._asked_instead_of_acting("Done. Anything else you'd like changed?"))

    def test_signoff_not_flagged(self):
        self.assertFalse(O._asked_instead_of_acting("Let me know if you have any other questions."))

    def test_needs_a_question_mark(self):
        # A clarification phrase without an actual question isn't a punt.
        self.assertFalse(O._asked_instead_of_acting("I need more detail before I can finish this."))

    def test_empty(self):
        self.assertFalse(O._asked_instead_of_acting(""))


class ClarifyLoopTest(unittest.TestCase):
    def test_loop_nudges_then_lets_the_answer_through(self):
        # Turn 1 punts with a clarifying question; the loop should inject the nudge and
        # give the model another turn (where it then answers), rather than finishing on
        # the questions.
        seen = {"convs": []}

        class P:
            name, api_key = "fake", "x"
            def __init__(self): self.i = 0
            def is_available(self): return True
            def list_models(self): return ["m"]
            def stream(self, conv, model, tools, on_text, *, cancel=None):
                self.i += 1
                seen["convs"].append(list(conv.messages))
                if self.i == 1:
                    return ProviderResponse(message=Message.assistant(
                        text="I need more detail. Could you specify which file you'd like me to modify?"), raw={})
                on_text("done")
                return ProviderResponse(message=Message.assistant(text="done"), raw={})

        s = Session(default_model="fake:m")
        task = Task(description="add a helper")
        orchestrator.run_task(s, task, lambda e: None, {"fake": P()})
        self.assertGreaterEqual(len(seen["convs"]), 2)                  # got a second turn
        second = " ".join((m.text or "") for m in seen["convs"][1])
        self.assertIn("Don't ask the user to clarify", second)          # nudge was injected


if __name__ == "__main__":
    unittest.main()
