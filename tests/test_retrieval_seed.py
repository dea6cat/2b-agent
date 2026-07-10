"""retrieval seeding — identifiers from task text -> definition + lexical seed files.
Run: `python -m unittest tests.test_retrieval_seed`.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import retrieval  # noqa: E402


class Seed(unittest.TestCase):
    def setUp(self):
        os.environ["TWOB_NO_LSP"] = "1"   # deterministic: force the regex definition floor
        self.addCleanup(lambda: os.environ.pop("TWOB_NO_LSP", None))

    def _proj(self, files):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for name, content in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(name) else None
            with open(p, "w") as f:
                f.write(content)
        return d

    def test_identifiers_extracted(self):
        ids = retrieval.task_identifiers("Fix the AuthService.login flow and Session refresh")
        self.assertIn("AuthService", ids)
        self.assertIn("Session", ids)
        self.assertNotIn("the", ids)      # common words dropped

    def test_prose_words_are_not_identifiers(self):
        # Lowercase prose must not be treated as code identifiers (else every word triggers a
        # full-tree definitions walk). Only CamelCase / snake_case / Capitalized tokens survive.
        ids = retrieval.task_identifiers("investigate why the payment processing drops retry attempts")
        self.assertEqual(ids, [])
        ids2 = retrieval.task_identifiers("update parse_config and the HttpClient wrapper")
        self.assertIn("parse_config", ids2)   # snake_case
        self.assertIn("HttpClient", ids2)     # CamelCase
        self.assertNotIn("update", ids2)      # lowercase prose
        self.assertNotIn("wrapper", ids2)

    def test_definition_seed(self):
        d = self._proj({"lib/auth.py": "class AuthService:\n    def login(self):\n        pass\n"})
        g = retrieval.build_graph(d)
        seeds, ids = retrieval.seeds_from_task("fix AuthService login", d, g)
        self.assertIn(os.path.join("lib", "auth.py"), seeds)
        self.assertIn("AuthService", ids)

    def test_lexical_seed_by_path(self):
        d = self._proj({"lib/payment_gateway.py": "x = 1\n", "lib/other.py": "y = 2\n"})
        g = retrieval.build_graph(d)
        seeds, _ = retrieval.seeds_from_task("investigate the payment gateway timeout", d, g)
        self.assertIn(os.path.join("lib", "payment_gateway.py"), seeds)

    def test_no_seed_returns_empty(self):
        d = self._proj({"lib/a.py": "x=1\n"})
        g = retrieval.build_graph(d)
        seeds, _ = retrieval.seeds_from_task("something totally unrelated zzz", d, g)
        self.assertEqual(seeds, set())

    def test_candidates_include_graph_neighbors(self):
        d = self._proj({"a.py": "class Widget:\n    pass\n", "b.py": "import a\n"})
        g = retrieval.build_graph(d)
        seeds, _ = retrieval.seeds_from_task("edit Widget", d, g)
        cands = retrieval.candidate_files(g, seeds)
        self.assertIn("a.py", cands)      # seed
        self.assertIn("b.py", cands)      # imports the seed (dependent)


if __name__ == "__main__":
    unittest.main()
