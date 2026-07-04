"""Tests for P8 — project-instructions injection. A project-root CLAUDE.md (fallback
AGENTS.md) is read once and folded into the system prompt under a `### project
instructions` header, size-capped, without being double-injected as the project map.
Host-side. Run: `python -m unittest tests.test_project_instructions`.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator  # noqa: E402


class ProjectInstructions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="2b-instr-")
        self.prev = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(lambda: os.chdir(self.prev))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def _write(self, name, content):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(content)

    def test_reads_claude_md(self):
        self._write("CLAUDE.md", "Use tabs. Never use var.")
        self.assertEqual(orchestrator._project_instructions(), "Use tabs. Never use var.")

    def test_falls_back_to_agents_md(self):
        self._write("AGENTS.md", "Run the linter before finishing.")
        self.assertEqual(orchestrator._project_instructions(), "Run the linter before finishing.")

    def test_claude_wins_over_agents(self):
        self._write("CLAUDE.md", "primary rules")
        self._write("AGENTS.md", "fallback rules")
        self.assertEqual(orchestrator._project_instructions(), "primary rules")

    def test_none_present_is_empty(self):
        self.assertEqual(orchestrator._project_instructions(), "")

    def test_skip_prevents_double_injection_of_agents(self):
        # AGENTS.md is a fallback for BOTH the map and the instructions. When it's already
        # used as the map, the instructions slot must not read it again.
        self._write("AGENTS.md", "shared file")
        _text, name = orchestrator._project_context()
        self.assertEqual(name, "AGENTS.md")                       # map consumed AGENTS.md
        self.assertEqual(orchestrator._project_instructions(skip=name), "")

    def test_claude_still_used_even_if_agents_is_the_map(self):
        # If a real CLAUDE.md exists, skipping AGENTS.md must not suppress it.
        self._write("AGENTS.md", "map/layout doc")
        self._write("CLAUDE.md", "coding rules")
        _text, name = orchestrator._project_context()
        self.assertEqual(name, "AGENTS.md")
        self.assertEqual(orchestrator._project_instructions(skip=name), "coding rules")

    def test_size_capped(self):
        big = "x" * (orchestrator.PROJECT_INSTRUCTIONS_MAX + 500)
        self._write("CLAUDE.md", big)
        out = orchestrator._project_instructions()
        self.assertLess(len(out), len(big))
        self.assertTrue(out.endswith("[truncated]"))

    def test_2b_md_is_map_not_instructions(self):
        # 2B.md is the /init map; it must NOT be picked up as instructions.
        self._write("2B.md", "project map content")
        _text, name = orchestrator._project_context()
        self.assertEqual(name, "2B.md")
        self.assertEqual(orchestrator._project_instructions(skip=name), "")


if __name__ == "__main__":
    unittest.main()
