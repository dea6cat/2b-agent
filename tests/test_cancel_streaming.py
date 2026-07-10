"""stream_with_retry forwards cancel to the provider and never retries a _Cancelled.

Run: `python -m unittest tests.test_cancel_streaming` from the repo root.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers import base  # noqa: E402
from two_b.providers.base import _Cancelled, ProviderError  # noqa: E402


class ForwardsCancel(unittest.TestCase):
    def test_cancel_is_forwarded_to_provider_stream(self):
        seen = {}

        class P:
            name = "p"

            def stream(self, conv, model, tools, on_text, *, cancel=None, **_kwargs):
                seen["cancel"] = cancel
                return "ok"

        ev = threading.Event()
        base.stream_with_retry(P(), None, "m", (), lambda c: None, cancel=ev)
        self.assertIs(seen["cancel"], ev)

    def test_cancelled_is_not_retried(self):
        calls = {"n": 0}

        class P:
            name = "p"

            def stream(self, conv, model, tools, on_text, *, cancel=None, **_kwargs):
                calls["n"] += 1
                raise _Cancelled()

        with self.assertRaises(_Cancelled):
            base.stream_with_retry(P(), None, "m", (), lambda c: None, cancel=threading.Event())
        self.assertEqual(calls["n"], 1, "a cancelled stream must not be retried")

    def test_retryable_provider_error_still_retries(self):
        calls = {"n": 0}

        class P:
            name = "p"

            def stream(self, conv, model, tools, on_text, *, cancel=None, **_kwargs):
                calls["n"] += 1
                raise ProviderError("p", "boom", retryable=True)

        with self.assertRaises(ProviderError):
            base.stream_with_retry(P(), None, "m", (), lambda c: None, retries=1, cancel=None)
        self.assertEqual(calls["n"], 2, "one initial try + one retry")


if __name__ == "__main__":
    unittest.main()
