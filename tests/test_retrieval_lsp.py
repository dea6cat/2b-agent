"""lsp.references + retrieval seed enrichment — degrade cleanly without a language server.
Run: `python -m unittest tests.test_retrieval_lsp`.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import lsp, retrieval  # noqa: E402


class Refs(unittest.TestCase):
    def test_references_none_when_lsp_disabled(self):
        os.environ["TWOB_NO_LSP"] = "1"
        self.addCleanup(lambda: os.environ.pop("TWOB_NO_LSP", None))
        self.assertIsNone(lsp.references("AuthService", "."))

    def test_enrich_is_noop_without_lsp(self):
        os.environ["TWOB_NO_LSP"] = "1"
        self.addCleanup(lambda: os.environ.pop("TWOB_NO_LSP", None))
        base = {"a.py"}
        out = retrieval.enrich_seeds_with_refs(set(base), ["AuthService"], ".", deadline=time.monotonic() + 1)
        self.assertEqual(out, base)                # nothing added, no crash

    def test_enrich_respects_expired_deadline(self):
        # An already-passed deadline must skip LSP entirely and return the seeds unchanged.
        out = retrieval.enrich_seeds_with_refs({"a.py"}, ["X"], ".", deadline=time.monotonic() - 1)
        self.assertEqual(out, {"a.py"})


if __name__ == "__main__":
    unittest.main()
