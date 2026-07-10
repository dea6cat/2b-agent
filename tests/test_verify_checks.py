"""verify.discover_checks tiers + new-language detection + TWOB_VERIFY_CMD override.
Run: `python -m unittest tests.test_verify_checks` from the repo root.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import verify  # noqa: E402


class Detect(unittest.TestCase):
    def _proj(self, files: dict) -> str:
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        for name, content in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(name) else None
            with open(p, "w") as f:
                f.write(content)
        return d

    def test_go(self):
        d = self._proj({"go.mod": "module x\n"})
        self.assertEqual(verify.discover_checks(d),
                         [("go build ./...", "fast"), ("go test ./...", "tests")])

    def test_swift(self):
        d = self._proj({"Package.swift": "// swift-tools-version:5.9\n"})
        self.assertEqual(verify.discover_checks(d),
                         [("swift build", "fast"), ("swift test", "tests")])

    def test_kotlin_gradle_check_is_single_test_inclusive(self):
        d = self._proj({"build.gradle.kts": "", "gradlew": "#!/bin/sh\n"})
        checks = verify.discover_checks(d)
        self.assertEqual(checks, [("./gradlew check", "tests")])  # single, test-inclusive, no fast, no separate test

    def test_kotlin_without_wrapper_uses_gradle(self):
        d = self._proj({"build.gradle": ""})
        self.assertEqual(verify.discover_checks(d), [("gradle check", "tests")])

    def test_ts_tsc_fallback_only_without_script(self):
        d = self._proj({"package.json": json.dumps({"scripts": {}}), "tsconfig.json": "{}"})
        self.assertIn(("tsc --noEmit", "fast"), verify.discover_checks(d))

    def test_ts_no_dupe_when_typecheck_script_present(self):
        d = self._proj({"package.json": json.dumps({"scripts": {"typecheck": "tsc"}}), "tsconfig.json": "{}"})
        checks = verify.discover_checks(d)
        self.assertIn(("npm run typecheck", "fast"), checks)
        self.assertNotIn(("tsc --noEmit", "fast"), checks)

    def test_eslint_fallback_only_without_lint_script(self):
        d = self._proj({"package.json": json.dumps({"scripts": {}}), ".eslintrc.json": "{}"})
        self.assertIn(("eslint .", "fast"), verify.discover_checks(d))

    def test_npm_test_is_tests_kind(self):
        d = self._proj({"package.json": json.dumps({"scripts": {"test": "jest"}})})
        self.assertIn(("npm run test", "tests"), verify.discover_checks(d))

    def test_unknown_project_empty(self):
        self.assertEqual(verify.discover_checks(self._proj({"README.md": "x"})), [])

    def test_classify_fallback(self):
        self.assertEqual(verify.classify("swift test"), "tests")
        self.assertEqual(verify.classify("go test ./..."), "tests")
        self.assertEqual(verify.classify("swift build"), "fast")
        self.assertEqual(verify.classify("eslint ."), "fast")
        self.assertEqual(verify.classify("make widget"), "fast")  # unmatched -> fast

    def test_override_env(self):
        os.environ["TWOB_VERIFY_CMD"] = "make lint;;make test"
        self.addCleanup(os.environ.pop, "TWOB_VERIFY_CMD", None)
        self.assertEqual(verify.discover_or_override(self._proj({})),
                         [("make lint", "fast"), ("make test", "tests")])


if __name__ == "__main__":
    unittest.main()
