"""Startup warning when 2B is launched from the home folder, where a bare search_files /
list_files walks everything under ~ (slow, and rarely what a coding task wants).
Run: `python -m unittest tests.test_home_warning`.
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rich.console import Console  # noqa: E402

from two_b.ui import banner  # noqa: E402

try:
    from two_b.ui.app_tui import TwoBApp  # noqa: E402
except ModuleNotFoundError:
    TwoBApp = None


class HomeWarning(unittest.TestCase):
    def test_home_folder_warns(self):
        self.assertIn("home folder", banner.home_warning(os.path.expanduser("~")))

    def test_project_folder_is_silent(self):
        self.assertEqual(banner.home_warning(tempfile.mkdtemp()), "")

    def test_missing_folder_is_silent(self):
        self.assertEqual(banner.home_warning("/no/such/dir/2b"), "")

    def test_classic_banner_shows_it_only_at_home(self):
        def rendered(cwd):
            con = Console(record=True, width=200)
            banner.render(con, "m", cwd)
            return con.export_text()
        self.assertIn("home folder", rendered(os.path.expanduser("~")))
        self.assertNotIn("home folder", rendered(tempfile.mkdtemp()))

    @unittest.skipIf(TwoBApp is None, "textual not installed (runtime-only dependency)")
    def test_tui_intro_shows_it_only_at_home(self):
        def intro(cwd):
            stub = SimpleNamespace(session=SimpleNamespace(cwd=cwd), c=lambda role: "white")
            return " ".join(str(t) for t in TwoBApp._intro_lines(stub))
        self.assertIn("home folder", intro(os.path.expanduser("~")))
        self.assertNotIn("home folder", intro(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()
