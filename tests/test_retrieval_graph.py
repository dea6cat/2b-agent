"""retrieval.build_graph — regex import graph + BFS distance + cache.
Run: `python -m unittest tests.test_retrieval_graph`.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import retrieval  # noqa: E402


class Graph(unittest.TestCase):
    def _proj(self, files: dict) -> str:
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for name, content in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(name) else None
            with open(p, "w") as f:
                f.write(content)
        return d

    def test_python_relative_and_package_imports(self):
        d = self._proj({
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\nimport pkg.c\n",
            "pkg/b.py": "x = 1\n",
            "pkg/c.py": "y = 2\n",
        })
        g = retrieval.build_graph(d)
        self.assertIn(os.path.join("pkg", "b.py"), g.imports[os.path.join("pkg", "a.py")])
        self.assertIn(os.path.join("pkg", "c.py"), g.imports[os.path.join("pkg", "a.py")])
        # reverse edges
        self.assertIn(os.path.join("pkg", "a.py"), g.imported_by[os.path.join("pkg", "b.py")])

    def test_js_relative_import(self):
        d = self._proj({"src/app.js": "import {x} from './util';\n", "src/util.js": "export const x=1;\n"})
        g = retrieval.build_graph(d)
        self.assertIn(os.path.join("src", "util.js"), g.imports[os.path.join("src", "app.js")])

    def test_unresolved_import_produces_no_edge(self):
        d = self._proj({"a.py": "import nonexistent_external_pkg\n"})
        g = retrieval.build_graph(d)
        self.assertEqual(g.imports.get("a.py", set()), set())

    def test_bfs_distance_both_directions(self):
        d = self._proj({
            "a.py": "import b\n", "b.py": "import c\n", "c.py": "z=1\n", "d.py": "q=1\n",
        })
        g = retrieval.build_graph(d)
        dist = retrieval.bfs_distances(g, {"b.py"}, radius=2)
        self.assertEqual(dist["b.py"], 0)
        self.assertEqual(dist["a.py"], 1)   # a imports b (predecessor)
        self.assertEqual(dist["c.py"], 1)   # b imports c (successor)
        self.assertNotIn("d.py", dist)      # unconnected

    def test_cache_reuses_until_signature_changes(self):
        d = self._proj({"a.py": "x=1\n"})
        g1 = retrieval.build_graph(d)
        g2 = retrieval.build_graph(d)
        self.assertIs(g1, g2)               # same signature -> cached instance
        with open(os.path.join(d, "b.py"), "w") as f:
            f.write("import a\n")
        g3 = retrieval.build_graph(d)
        self.assertIsNot(g1, g3)            # tree changed -> rebuilt

    def test_never_raises_on_bad_tree(self):
        g = retrieval.build_graph("/no/such/dir/xyz")
        self.assertEqual(g.files, set())


if __name__ == "__main__":
    unittest.main()
