import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator


class Exposure(unittest.TestCase):
    def test_local_has_no_delegate(self):
        names = [s.name for s in orchestrator._active_specs(is_local=True)]
        self.assertNotIn("delegate", names)
        self.assertEqual(
            names[:6],
            ["list_files", "read_file", "search_files", "edit_file", "write_file", "run_git"],
        )

    def test_cloud_has_delegate(self):
        names = [s.name for s in orchestrator._active_specs(is_local=False)]
        self.assertIn("delegate", names)

    def test_frozen_schema_still_holds(self):
        # two_b.tools import triggers the toolspec.py assert (to_openai() == tools.TOOLS)
        import two_b.tools  # noqa: F401


if __name__ == "__main__":
    unittest.main()
