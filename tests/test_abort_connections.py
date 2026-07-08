"""Tests for the abortable HTTP layer: a set cancel flag short-circuits before
connecting, abort_all_connections() closes live responses, and a socket closed
mid-read surfaces as _Cancelled (not a retryable ProviderError).

Run: `python -m unittest tests.test_abort_connections` from the repo root.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers import base  # noqa: E402
from two_b.providers.base import _Cancelled, ProviderError  # noqa: E402


class FakeResp:
    """Stand-in for a urllib response: iterable of byte lines, closeable, and it
    raises ValueError on read once closed (matching a closed socket)."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.closed = False

    def __iter__(self):
        for ln in self._lines:
            if self.closed:
                raise ValueError("read of closed file")
            yield ln

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self):
        if self.closed:
            raise ValueError("read of closed file")
        return b"".join(self._lines)

    def close(self):
        self.closed = True


class AbortLayer(unittest.TestCase):
    def tearDown(self):
        base.abort_all_connections()  # never leak a registered fake between tests

    def test_preset_cancel_short_circuits_post_stream(self):
        cancel = threading.Event()
        cancel.set()
        gen = base.post_stream("http://x", {}, cancel=cancel)
        with self.assertRaises(_Cancelled):
            next(gen)

    def test_preset_cancel_short_circuits_post_json(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(_Cancelled):
            base.post_json("http://x", {}, cancel=cancel)

    def test_abort_closes_registered_response(self):
        resp = FakeResp([b"a\n", b"b\n"])
        base._register(resp)
        base.abort_all_connections()
        self.assertTrue(resp.closed)

    def test_close_mid_stream_with_cancel_set_raises_cancelled(self):
        cancel = threading.Event()
        resp = FakeResp([b"one\n", b"two\n", b"three\n"])

        def fake_urlopen(req, timeout=600):
            return resp

        real = base.urllib.request.urlopen
        base.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, base.urllib.request, "urlopen", real)

        gen = base.post_stream("http://x", {}, cancel=cancel)
        self.assertEqual(next(gen), "one\n")     # first line arrives normally
        cancel.set()
        base.abort_all_connections()             # close the socket out from under it
        with self.assertRaises(_Cancelled):
            next(gen)

    def test_close_mid_stream_without_cancel_is_a_provider_error(self):
        # A drop we did NOT initiate must stay a retryable ProviderError, not _Cancelled.
        # Needs a 2nd line: closing right after the *last* line is indistinguishable
        # from a clean stream end (FakeResp only checks `closed` on its next iteration).
        resp = FakeResp([b"one\n", b"two\n"])

        def fake_urlopen(req, timeout=600):
            return resp

        real = base.urllib.request.urlopen
        base.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, base.urllib.request, "urlopen", real)

        gen = base.post_stream("http://x", {}, provider="p")
        self.assertEqual(next(gen), "one\n")
        resp.close()                              # external drop, no cancel flag
        with self.assertRaises(ProviderError):
            next(gen)


if __name__ == "__main__":
    unittest.main()
