"""retrieval.retrieve_block — end-to-end pointer block: budget, fence, confidence gate, opt-out.
Run: `python -m unittest tests.test_retrieval_block`.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import retrieval  # noqa: E402


class Block(unittest.TestCase):
    def setUp(self):
        os.environ["TWOB_NO_LSP"] = "1"
        self.addCleanup(lambda: os.environ.pop("TWOB_NO_LSP", None))
        self.addCleanup(lambda: os.environ.pop("TWOB_NO_RETRIEVAL", None))

    def _proj(self, files):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for name, content in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(name) else None
            with open(p, "w") as f:
                f.write(content)
        return d

    def test_block_lists_relevant_file_and_is_fenced(self):
        # 3+ declarations so symbols.outline() (which skips sparse < 3-symbol files) fences something.
        d = self._proj({"lib/auth.py": "class AuthService:\n    def login(self):\n        pass\n\n    def logout(self):\n        pass\n"})
        block = retrieval.retrieve_block(d, "fix AuthService login")
        self.assertIn("auth.py", block)
        self.assertIn("<untrusted_data", block)          # outline fenced
        self.assertLessEqual(len(block), retrieval.RETRIEVAL_CHAR_BUDGET + 400)  # budget + header slack

    def test_confidence_gate_no_match_returns_empty(self):
        d = self._proj({"lib/a.py": "x=1\n"})
        self.assertEqual(retrieval.retrieve_block(d, "totally unrelated zzz request"), "")

    def test_opt_out(self):
        os.environ["TWOB_NO_RETRIEVAL"] = "1"
        d = self._proj({"lib/auth.py": "class AuthService:\n    pass\n"})
        self.assertEqual(retrieval.retrieve_block(d, "fix AuthService"), "")

    def test_never_raises(self):
        self.assertEqual(retrieval.retrieve_block("/no/such/dir", "anything"), "")


if __name__ == "__main__":
    unittest.main()
