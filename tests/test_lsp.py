"""Tests for the stdlib LSP client.

The wire framing is tested deterministically (no server needed); a real server is
exercised only when one is installed, and leniently — the client must never raise and
must degrade to None, so these assert behavior/safety, not a specific server's output.
Run: `python -m unittest tests.test_lsp` from the repo root.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import lsp  # noqa: E402


class Framing(unittest.TestCase):
    def test_encode_read_roundtrip(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"x": [1, 2]}}
        frame = lsp.encode(msg)
        self.assertIn(b"Content-Length:", frame)
        self.assertEqual(lsp.read_frame(io.BytesIO(frame)), msg)

    def test_read_two_messages_from_one_stream(self):
        stream = io.BytesIO(lsp.encode({"id": 1}) + lsp.encode({"id": 2}))
        self.assertEqual(lsp.read_frame(stream)["id"], 1)
        self.assertEqual(lsp.read_frame(stream)["id"], 2)
        self.assertIsNone(lsp.read_frame(stream))          # EOF

    def test_read_frame_eof_and_malformed(self):
        self.assertIsNone(lsp.read_frame(io.BytesIO(b"")))
        self.assertIsNone(lsp.read_frame(io.BytesIO(b"Content-Length: 5\r\n\r\nab")))  # short body
        self.assertIsNone(lsp.read_frame(io.BytesIO(b"Content-Length: x\r\n\r\n")))    # bad length

    def test_uri_roundtrip(self):
        p = os.path.abspath(__file__)
        self.assertEqual(os.path.realpath(lsp._from_uri(lsp._to_uri(p))), os.path.realpath(p))

    def test_server_spec_detection(self):
        # Whatever the machine has, the return is either None or a (argv_tuple, lang) pair.
        spec = lsp._server_spec(".py")
        self.assertTrue(spec is None or (isinstance(spec[0], tuple) and isinstance(spec[1], str)))
        self.assertIsNone(lsp._server_spec(".unknownext"))


@unittest.skipUnless(shutil.which("clangd"), "clangd not installed")
class RealServer(unittest.TestCase):
    def tearDown(self):
        lsp.shutdown_all()

    def test_initializes_and_never_raises(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.c"), "w") as f:
            f.write("int add(int a, int b) { return a + b; }\n")
        srv = lsp._get_server(("clangd",), d, "c")
        self.assertIsNotNone(srv)
        self.assertTrue(srv.ok)                            # handshake completed over real stdio
        # definitions must return None or a list — never raise, whatever clangd does.
        result = lsp.definitions("add", d)
        self.assertTrue(result is None or isinstance(result, list))


if __name__ == "__main__":
    unittest.main()
