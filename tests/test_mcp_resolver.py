"""Tests for the host-consumed MCP symbol-resolver backend.

The flattened-text location parser is tested deterministically; tier integration is
tested by faking manager.resolve_symbol (no real MCP server needed). Run:
`python -m unittest tests.test_mcp_resolver` from the repo root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import mcp_client, symbols  # noqa: E402


class LocationParser(unittest.TestCase):
    def test_parses_file_line_pairs(self):
        text = "Widget lib/ui/widget.dart:42:8\nalso lib/ui/widget.dart:42:8\nmore src/a.py:7"
        self.assertEqual(
            mcp_client._parse_locations(text),
            [("lib/ui/widget.dart", 42), ("src/a.py", 7)],   # deduped, col ignored
        )

    def test_no_locations_returns_empty(self):
        self.assertEqual(mcp_client._parse_locations("no coordinates here"), [])

    def test_cap_is_respected(self):
        text = "".join(f"f{i}.py:{i}\n" for i in range(50))
        self.assertEqual(len(mcp_client._parse_locations(text, cap=10)), 10)


class _FakeTool:
    def __init__(self, props):
        self.inputSchema = {"properties": props}


class ResolverArg(unittest.TestCase):
    def test_prefers_known_query_names(self):
        self.assertEqual(mcp_client._resolver_arg(_FakeTool({"query": {}})), "query")
        self.assertEqual(mcp_client._resolver_arg(_FakeTool({"name": {}})), "name")

    def test_falls_back_to_default(self):
        self.assertEqual(mcp_client._resolver_arg(_FakeTool({"unrelated": {}})), "query")


class TierIntegration(unittest.TestCase):
    """symbols.definitions() should use the MCP backend when LSP finds nothing (no server
    installed for a throwaway cwd) and regex would otherwise be the floor."""

    def setUp(self):
        self._orig = mcp_client.manager.resolve_symbol

    def tearDown(self):
        mcp_client.manager.resolve_symbol = self._orig

    def test_mcp_result_flows_through(self):
        mcp_client.manager.resolve_symbol = lambda ident, timeout=8: [("pkg/thing.dart", 12)]
        locs = symbols.definitions("Thing", "/nonexistent-project-root")
        self.assertEqual(len(locs), 1)
        self.assertEqual((locs[0].path, locs[0].line, locs[0].text), ("pkg/thing.dart", 12, "Thing"))

    def test_mcp_none_defers_to_regex_floor(self):
        # No resolver -> None; with no LSP server and an empty/junk cwd, regex yields [].
        mcp_client.manager.resolve_symbol = lambda ident, timeout=8: None
        self.assertEqual(symbols.definitions("Nope", "/nonexistent-project-root"), [])


if __name__ == "__main__":
    unittest.main()
