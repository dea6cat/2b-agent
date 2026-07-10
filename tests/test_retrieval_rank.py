"""retrieval.rank — weighted score (graph + lexical + prior), ordering, confidence gate.
Run: `python -m unittest tests.test_retrieval_rank`.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import retrieval  # noqa: E402


class Rank(unittest.TestCase):
    def setUp(self):
        os.environ["TWOB_NO_LSP"] = "1"
        self.addCleanup(lambda: os.environ.pop("TWOB_NO_LSP", None))
        self.addCleanup(lambda: os.environ.pop("TWOB_RETRIEVAL_FILES", None))

    def _proj(self, files):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for name, content in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(name) else None
            with open(p, "w") as f:
                f.write(content)
        return d

    def test_seed_and_dependent_rank_above_unrelated(self):
        d = self._proj({
            "lib/auth.py": "class AuthService:\n    def login(self):\n        pass\n",
            "lib/login_view.py": "import lib.auth\n",
            "lib/unrelated.py": "class Zebra:\n    pass\n",
        })
        g = retrieval.build_graph(d)
        seeds, ids = retrieval.seeds_from_task("fix AuthService login", d, g)
        ranked = retrieval.rank("fix AuthService login", d, g, seeds, ids, k=10)
        paths = [r.path for r in ranked]
        self.assertEqual(paths[0], os.path.join("lib", "auth.py"))      # the definition, top
        self.assertIn(os.path.join("lib", "login_view.py"), paths)      # dependent, included
        self.assertNotIn(os.path.join("lib", "unrelated.py"), paths)    # unconnected + no match

    def test_reasons_present(self):
        d = self._proj({"lib/auth.py": "class AuthService:\n    pass\n"})
        g = retrieval.build_graph(d)
        seeds, ids = retrieval.seeds_from_task("AuthService", d, g)
        ranked = retrieval.rank("AuthService", d, g, seeds, ids, k=5)
        self.assertTrue(any("defines" in r.reasons[0] or "matches" in r.reasons[0] for r in ranked))

    def test_defines_reason_has_no_trailing_colon(self):
        d = self._proj({"lib/auth.py": "class AuthService:\n    pass\n"})
        g = retrieval.build_graph(d)
        seeds, ids = retrieval.seeds_from_task("AuthService", d, g)
        ranked = retrieval.rank("AuthService", d, g, seeds, ids, k=5)
        auth = next(r for r in ranked if r.path.endswith("auth.py"))
        self.assertIn("defines AuthService", auth.reasons)
        self.assertNotIn("defines AuthService:", auth.reasons)   # no leaked ':'

    def test_dependent_neighbor_reason_direction(self):
        # login_view imports auth. Seeding ONLY on auth (the definition), the dependent
        # login_view must be described as importing auth — the correct edge direction.
        d = self._proj({
            "lib/auth.py": "class AuthService:\n    pass\n",
            "lib/login_view.py": "import lib.auth\n",
        })
        g = retrieval.build_graph(d)
        # force the seed set to just the definition file (isolate the d>0 branch)
        seeds = {os.path.join("lib", "auth.py")}
        ranked = retrieval.rank("AuthService", d, g, seeds, ["AuthService"], k=10)
        lv = next(r for r in ranked if r.path.endswith("login_view.py"))
        self.assertIn("imports auth.py", lv.reasons)             # login_view imports the seed
        self.assertNotIn("imported by auth.py", lv.reasons)      # NOT the reverse

    def test_top_k_env(self):
        os.environ["TWOB_RETRIEVAL_FILES"] = "3"
        self.assertEqual(retrieval.top_k(), 3)
        os.environ.pop("TWOB_RETRIEVAL_FILES")
        self.assertEqual(retrieval.top_k(), retrieval.DEFAULT_K)


if __name__ == "__main__":
    unittest.main()
