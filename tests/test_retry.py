import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from two_b.providers import base
from two_b.providers.base import ProviderError, ProviderResponse
from two_b.conversation import Conversation, Message
class Retry(unittest.TestCase):
    def setUp(self):
        self._orig_sleep = base._time.sleep
        base._time.sleep = lambda *_: None   # no real backoff in tests

    def tearDown(self):
        base._time.sleep = self._orig_sleep   # base._time IS the stdlib time module;
        # leaving it patched would break other tests' real-timing assertions.
    def test_retries_then_succeeds(self):
        calls = {"n": 0}
        class P:
            name="x"
            def stream(self, c, m, t, on_text, *, cancel=None):
                calls["n"] += 1
                if calls["n"] < 3: raise ProviderError("x", "HTTP 429: slow down", retryable=True)
                return ProviderResponse(message=Message.assistant(text="ok"), raw={})
        r = base.stream_with_retry(P(), Conversation(system_prompt="s"), "m", (), lambda _c: None, retries=3)
        self.assertEqual(r.message.text, "ok"); self.assertEqual(calls["n"], 3)
    def test_non_retryable_raises_immediately(self):
        class P:
            name="x"
            def stream(self, c,m,t,on_text, *, cancel=None): raise ProviderError("x","HTTP 400: bad", retryable=False)
        with self.assertRaises(ProviderError):
            base.stream_with_retry(P(), Conversation(system_prompt="s"), "m", (), lambda _c: None, retries=3)

    def test_exhausted_retries_annotates_message(self):
        class P:
            name="nvidia"
            def stream(self, c,m,t,on_text, *, cancel=None): raise ProviderError("nvidia","HTTP 504: Gateway Timeout", retryable=True)
        with self.assertRaises(ProviderError) as ctx:
            base.stream_with_retry(P(), Conversation(system_prompt="s"), "m", (), lambda _c: None, retries=3)
        # Provider prefix isn't duplicated, and the retry count is surfaced.
        self.assertEqual(str(ctx.exception), "[nvidia] HTTP 504: Gateway Timeout — retried 3×, still failing")

    def test_no_retries_left_is_not_annotated(self):
        # retries=0 means we never actually retried, so no "retried N×" suffix.
        class P:
            name="x"
            def stream(self, c,m,t,on_text, *, cancel=None): raise ProviderError("x","HTTP 503: down", retryable=True)
        with self.assertRaises(ProviderError) as ctx:
            base.stream_with_retry(P(), Conversation(system_prompt="s"), "m", (), lambda _c: None, retries=0)
        self.assertEqual(str(ctx.exception), "[x] HTTP 503: down")
