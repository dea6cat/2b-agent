import os, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from two_b.core.skills import parse_frontmatter, get_all_skills, substitute_arguments, parse_argument_names


class Frontmatter(unittest.TestCase):
    def test_simple_key_value(self):
        fm, body = parse_frontmatter("---\nname: test\nenabled: true\n---\nHello")
        self.assertEqual(fm, {"name": "test", "enabled": True})
        self.assertEqual(body, "Hello")

    def test_integer(self):
        fm, _ = parse_frontmatter("---\ncount: 42\n---\nBody")
        self.assertEqual(fm, {"count": 42})

    def test_inline_list(self):
        fm, _ = parse_frontmatter("---\nitems: [a, b, c]\n---\nBody")
        self.assertEqual(fm, {"items": ["a", "b", "c"]})

    def test_hyphen_list(self):
        fm, _ = parse_frontmatter("---\nitems:\n  - a\n  - b\n---\nBody")
        self.assertEqual(fm, {"items": ["a", "b"]})

    def test_comma_separated(self):
        fm, _ = parse_frontmatter("---\nitems: a, b, c\n---\nBody")
        self.assertEqual(fm, {"items": ["a", "b", "c"]})

    def test_no_frontmatter(self):
        fm, body = parse_frontmatter("Just a body\nNo frontmatter here")
        self.assertEqual(fm, {})
        self.assertEqual(body, "Just a body\nNo frontmatter here")

    def test_boolean_false(self):
        fm, _ = parse_frontmatter("---\nflag: false\n---\nBody")
        self.assertEqual(fm, {"flag": False})


class ArgumentNames(unittest.TestCase):
    def test_list_input(self):
        self.assertEqual(parse_argument_names(["a", "b"]), ["a", "b"])

    def test_string_input(self):
        self.assertEqual(parse_argument_names("a b"), ["a", "b"])

    def test_none_returns_empty(self):
        self.assertEqual(parse_argument_names(None), [])

    def test_numbers_filtered(self):
        self.assertEqual(parse_argument_names(["x", "1"]), ["x"])


class Substitute(unittest.TestCase):
    def test_named_placeholders(self):
        result = substitute_arguments("Fix $target with $style", "src/main.py clean",
                                      argument_names=["target", "style"])
        self.assertEqual(result, "Fix src/main.py with clean")

    def test_positional_dollar(self):
        result = substitute_arguments("Do $1 then $2", "first second", argument_names=[])
        self.assertEqual(result, "Do first then second")

    def test_dollar_arguments(self):
        result = substitute_arguments("Args: $ARGUMENTS", "hello world", argument_names=[])
        self.assertEqual(result, "Args: hello world")

    def test_no_placeholder_appends(self):
        result = substitute_arguments("Do something", "extra args", argument_names=[])
        self.assertIn("ARGUMENTS: extra args", result)

    def test_none_args_returns_content_unchanged(self):
        result = substitute_arguments("Content", None, argument_names=[])
        self.assertEqual(result, "Content")


class SkillLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Isolate from the developer's real user skills (~/.clawd/skills, ~/.claude/skills,
        # $TWOB_SKILLS_DIR), which get_all_skills also loads — otherwise they leak into the counts.
        env = {k: v for k, v in os.environ.items() if k != "TWOB_SKILLS_DIR"}
        env["HOME"] = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_skill(self, name="test-skill", body="Do $task", **fm):
        d = os.path.join(self.tmp, ".clawd", "skills")
        os.makedirs(os.path.join(d, name), exist_ok=True)
        path = os.path.join(d, name, "SKILL.md")
        fm_str = "---\n"
        for k, v in fm.items():
            if isinstance(v, list):
                fm_str += f"{k}:\n"
                for item in v:
                    fm_str += f"  - {item}\n"
            else:
                fm_str += f"{k}: {v}\n"
        fm_str += "---\n"
        with open(path, "w") as f:
            f.write(fm_str + body)
        return path

    def test_load_from_project_dir(self):
        self._make_skill(description="A test skill")
        skills = get_all_skills(project_root=self.tmp)
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "test-skill")
        self.assertEqual(skills[0].description, "A test skill")

    def test_hidden_when_not_user_invocable(self):
        self._make_skill(description="Hidden skill", **{"user-invocable": "false"})
        skills = get_all_skills(project_root=self.tmp)
        self.assertEqual(len(skills), 0)

    def test_user_invocable_defaults_to_true(self):
        self._make_skill(description="Default visible")
        skills = get_all_skills(project_root=self.tmp)
        self.assertEqual(len(skills), 1)
        self.assertTrue(skills[0].user_invocable)


if __name__ == "__main__":
    unittest.main()
