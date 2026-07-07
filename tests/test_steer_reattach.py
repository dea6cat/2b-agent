"""Tests for steer re-attach: a steer typed on a task's last turn (no tool boundary left)
is carried into a continuation task that adopts the finished task's conversation, instead
of restarting from scratch. Host-side only.
Run: `python -m unittest tests.test_steer_reattach`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.conversation import Conversation, Message  # noqa: E402
from two_b.session import TaskState  # noqa: E402

# TwoBApp pulls in textual, a runtime-only dep the pure-Python test gate doesn't install.
# Skip cleanly when it's absent (like the bwrap tests on macOS) rather than erroring out.
try:
    from two_b.app_tui import TwoBApp  # noqa: E402
    _HAS_TEXTUAL = True
except ModuleNotFoundError:
    TwoBApp = None
    _HAS_TEXTUAL = False


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed (runtime-only dependency)")
class SteerReattach(unittest.IsolatedAsyncioTestCase):
    async def _app(self):
        return TwoBApp(model="fake:m", auto_yes=True, initial_task=None)

    async def test_leftover_steer_continues_same_conversation(self):
        app = await self._app()
        async with app.run_test():
            conv = Conversation(system_prompt="s")
            conv.append(Message.user("original request"))
            done = app.session.add_task("original request")
            done.conversation = conv
            done.edit_history = [("f.py", None)]
            done.state = TaskState.DONE
            done.push_steer("now also handle the error case")

            ran = []
            app._run = lambda t: ran.append(t)          # capture instead of spawning a worker
            app._flush_leftover_steer(done.id)

            self.assertEqual(len(ran), 1)
            cont = ran[0]
            self.assertIsNot(cont, done)                 # a continuation task, not the finished one
            self.assertIs(cont.conversation, conv)       # adopted the prior context
            self.assertEqual(cont.description, "now also handle the error case")
            self.assertEqual(cont.edit_history, [("f.py", None)])   # undo stack carried
            self.assertIsNot(cont.edit_history, done.edit_history)  # but copied, not aliased
            self.assertEqual(done.take_steer(), "")      # steer drained, not left dangling

    async def test_no_conversation_falls_back_to_fresh_task(self):
        app = await self._app()
        async with app.run_test():
            failed = app.session.add_task("bad request")
            failed.conversation = None                   # e.g. failed before building one
            failed.state = TaskState.ERROR
            failed.push_steer("try again differently")

            started = []
            app._start_task = lambda text: started.append(text)
            app._flush_leftover_steer(failed.id)

            self.assertEqual(started, ["try again differently"])   # plain new task
            self.assertEqual(failed.take_steer(), "")


if __name__ == "__main__":
    unittest.main()
