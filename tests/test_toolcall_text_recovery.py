import unittest
from two_b.tools import recover_toolcalls, loads_tolerant

KNOWN = ("read_file", "edit_file", "write_file", "search_files", "list_files", "run_git")


class RecoverToolcallsTest(unittest.TestCase):
    def test_fenced_json_block_from_qwen_coder(self):
        # Verbatim shape observed from qwen2.5-coder:14b in the measurement run.
        text = ('1. List all Dart source files under lib/.\n\n'
                '```json\n{\n  "name": "list_files",\n  "arguments": {\n    "path": "lib/"\n  }\n}\n```')
        self.assertEqual(recover_toolcalls(text, KNOWN), [("list_files", {"path": "lib/"})])

    def test_multiple_fenced_calls_in_one_message(self):
        text = ('```json\n{"name": "list_files", "arguments": {"path": "."}}\n```\n'
                '```json\n{"name": "read_file", "arguments": {"path": "README.md"}}\n```')
        self.assertEqual(recover_toolcalls(text, KNOWN),
                         [("list_files", {"path": "."}), ("read_file", {"path": "README.md"})])

    def test_whole_message_is_a_bare_json_call(self):
        self.assertEqual(recover_toolcalls('{"name": "search_files", "arguments": {"query": "TODO"}}', KNOWN),
                         [("search_files", {"query": "TODO"})])

    def test_name_key_variant_and_nested_arg_key(self):
        self.assertEqual(recover_toolcalls('{"tool": "read_file", "input": {"path": "b.py"}}', KNOWN),
                         [("read_file", {"path": "b.py"})])

    def test_unknown_tool_ignored(self):
        self.assertEqual(recover_toolcalls('{"name": "delete_everything", "arguments": {}}', KNOWN), [])

    def test_ordinary_prose_untouched(self):
        self.assertEqual(recover_toolcalls("I'll read the file and report back.", KNOWN), [])

    def test_empty(self):
        self.assertEqual(recover_toolcalls("", KNOWN), [])


class LoadsTolerantTest(unittest.TestCase):
    def test_valid_unchanged(self):
        self.assertEqual(loads_tolerant('{"path": "a.py"}'), {"path": "a.py"})

    def test_trailing_comma(self):
        self.assertEqual(loads_tolerant('{"path": "a.py",}'), {"path": "a.py"})

    def test_unclosed_object(self):
        self.assertEqual(loads_tolerant('{"path": "a.py"'), {"path": "a.py"})

    def test_hopeless_is_none(self):
        self.assertIsNone(loads_tolerant("not json"))
        self.assertIsNone(loads_tolerant(""))


if __name__ == "__main__":
    unittest.main()
