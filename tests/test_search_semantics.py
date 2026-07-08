"""Tests for host-side symbol enrichment folded into search_files / read_file.

The five-tool schema is frozen; these verify only that the *results* get richer —
definitions tagged and ranked, a bounded file outline appended — while non-identifier
queries, unsupported file types, section reads, and non-source reads stay byte-identical.
Run: `python -m unittest tests.test_search_semantics` from the repo root.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import repomap, symbols, tools  # noqa: E402


class Project(unittest.TestCase):
    """Each test runs inside a throwaway project dir (chdir'd) so relative paths and
    the project walk behave like a real session."""

    def setUp(self):
        self._prev = os.getcwd()
        self.dir = tempfile.mkdtemp()
        os.chdir(self.dir)

    def tearDown(self):
        os.chdir(self._prev)

    def _write(self, rel, text):
        full = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(rel) else None
        with open(full, "w") as f:
            f.write(text)
        return full


class DeclaredName(Project):
    def test_declared_name_extraction(self):
        self.assertEqual(repomap.declared_name("class Session:", ".py"), "Session")
        self.assertEqual(repomap.declared_name("def apply_edit(self, x):", ".py"), "apply_edit")
        self.assertEqual(repomap.declared_name("func Foo(a int) {", ".go"), "Foo")
        # A line that only *mentions* the type is not a definition of it.
        self.assertNotEqual(repomap.declared_name("def f(x: Session):", ".py"), "Session")


class SearchTagging(Project):
    def test_definition_tagged_and_ranked_first(self):
        self._write("uses.py", "s = Session()\nreturn Session\n")
        self._write("model.py", "class Session:\n    pass\n")
        out = tools.do_search_files("Session", ".")
        # matches are fenced as untrusted content; drop the fence marker lines
        lines = [l for l in out.splitlines() if not l.startswith(("<untrusted_data", "</untrusted_data"))]
        self.assertTrue(lines[0].startswith("▸def "))          # definition floated to top
        self.assertIn("class Session:", lines[0])
        self.assertEqual(sum(l.startswith("▸def ") for l in lines), 1)  # only the real def

    def test_non_identifier_query_untouched(self):
        self._write("a.py", "x = 1  # not found here\n")
        out = tools.do_search_files("not found", ".")          # phrase, not an identifier
        self.assertNotIn("▸def", out)
        self.assertNotIn("defined in:", out)
        self.assertIn("a.py", out)

    def test_unsupported_extension_untouched(self):
        self._write("notes.xyz", "class Thing:\n")             # .xyz has no declaration patterns
        out = tools.do_search_files("Thing", ".")
        self.assertNotIn("▸def", out)
        self.assertIn("notes.xyz", out)

    def test_no_matches_message(self):
        self._write("a.py", "y = 2\n")
        self.assertIn("no matches", tools.do_search_files("Nonexistent", "."))

    def test_overflow_stays_bounded(self):
        many = "".join(f"foo_ref_{i} = foo\n" for i in range(60))  # >30 usages of `foo`
        self._write("many.py", many)
        out = tools.do_search_files("foo", ".")
        self.assertIn("stopped after", out)
        # bounded: at most MAX_SEARCH_MATCHES match lines (+ optional header + note line)
        body = [l for l in out.splitlines() if l and not l.startswith(
            ("defined in:", "(stopped", "<untrusted_data", "</untrusted_data"))]
        self.assertLessEqual(len(body), tools.MAX_SEARCH_MATCHES)


class Resolver(Project):
    def test_definitions_resolves_across_files(self):
        self._write("pkg/impl.py", "class Widget:\n    pass\n")
        self._write("pkg/use.py", "w = Widget()\n")
        locs = symbols.definitions("Widget", self.dir)
        self.assertEqual(len(locs), 1)
        self.assertTrue(locs[0].path.endswith("impl.py"))
        self.assertEqual(locs[0].line, 1)

    def test_definitions_ignores_non_identifier(self):
        self._write("a.py", "class X:\n")
        self.assertEqual(symbols.definitions("class X", self.dir), [])


class ReadOutline(Project):
    _RICH = "def a():\n    pass\n\n\ndef b():\n    pass\n\n\nclass C:\n    def m(self):\n        pass\n"

    def test_outline_appended_with_line_numbers(self):
        p = self._write("rich.py", self._RICH)
        out = tools.do_read_file(p)
        self.assertIn("# symbols:", out)
        self.assertIn("1 def a():", out)
        self.assertIn("9 class C:", out)

    def test_outline_skipped_when_sparse(self):
        p = self._write("sparse.py", "def only():\n    pass\n")   # < 3 symbols
        self.assertNotIn("# symbols:", tools.do_read_file(p))

    def test_no_outline_on_section_read(self):
        self._write("rich.py", self._RICH)
        self.assertNotIn("# symbols:", tools.do_read_file("rich.py:1-3"))

    def test_no_outline_on_non_source(self):
        p = self._write("readme.txt", "def a():\n" * 5)          # .txt: no patterns
        self.assertNotIn("# symbols:", tools.do_read_file(p))

    def test_outline_never_breaches_max_chars(self):
        p = self._write("rich.py", self._RICH)
        content_len = len(self._RICH)
        out = tools.do_read_file(p, max_chars=content_len + 5)   # room for content, not outline
        self.assertNotIn("# symbols:", out)
        self.assertIn("class C:", out)                           # content itself intact


class SkipsDependencyDirs(Project):
    """The project walk (search/list and the read basename fallback) must not descend
    into virtualenv/cache trees, or an imprecise read resolves to a site-package file
    and search/list fill with dependency noise."""

    def test_basename_fallback_ignores_venv_matches(self):
        # A dependency file whose basename collides with the given name — the only
        # place "helper.py" exists — must NOT be resolved when it lives under .venv.
        self._write(".venv/lib/python3.11/site-packages/pkg/helper.py", "x = 1\n")
        self.assertEqual(tools._find_by_basename("helper.py", "helper.py"), [])
        self.assertIsNone(tools.resolve_read_path("/no/such/helper.py"))

    def test_real_project_file_still_wins_over_venv_namesake(self):
        # A real project file and a venv namesake: only the project one is found.
        self._write("app/helper.py", "y = 2\n")
        self._write(".venv/lib/python3.11/site-packages/pkg/helper.py", "x = 1\n")
        self.assertEqual(tools._find_by_basename("helper.py", "helper.py"),
                         [os.path.join("app", "helper.py")])

    def test_search_skips_venv(self):
        self._write("app/mod.py", "TOKEN = 1\n")
        self._write(".venv/lib/python3.11/site-packages/pkg/mod.py", "TOKEN = 2\n")
        out = tools.do_search_files("TOKEN", ".")
        self.assertIn(os.path.join("app", "mod.py"), out)
        self.assertNotIn(".venv", out)


if __name__ == "__main__":
    unittest.main()
