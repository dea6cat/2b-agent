"""Tests for subprocess environment hygiene (S2) and bounded child output.
Host-side. Run: `python -m unittest tests.test_env_scrub`.
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import tools  # noqa: E402


class EnvScrub(unittest.TestCase):
    def test_default_drops_secrets_keeps_tools(self):
        envmap = {
            "PATH": "/usr/bin", "HOME": "/h", "JAVA_HOME": "/j", "VIRTUAL_ENV": "/v",
            "AWS_SECRET_ACCESS_KEY": "x", "AWS_ACCESS_KEY_ID": "k", "GH_TOKEN": "y",
            "OPENAI_API_KEY": "z", "MY_PASSWORD": "p", "SSH_AUTH_SOCK": "/tmp/agent",
            "DATABASE_URL": "u",
        }
        with mock.patch.dict(os.environ, envmap, clear=True):
            env = tools._child_env()
        for secret in ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "GH_TOKEN",
                       "OPENAI_API_KEY", "MY_PASSWORD"):
            self.assertNotIn(secret, env, secret)
        for keep in ("PATH", "HOME", "JAVA_HOME", "VIRTUAL_ENV"):
            self.assertIn(keep, env, keep)
        self.assertIn("SSH_AUTH_SOCK", env)   # not a secret; needed for git-over-ssh
        self.assertIn("DATABASE_URL", env)    # keyword-less: kept by default (strict drops it)

    def test_strict_is_allowlist(self):
        envmap = {
            "PATH": "/usr/bin", "JAVA_HOME": "/j", "LC_ALL": "C", "GIT_AUTHOR_NAME": "a",
            "DATABASE_URL": "u", "GH_TOKEN": "y", "SOME_RANDOM_VAR": "r",
            "TWOB_SEATBELT": "strict",
        }
        with mock.patch.dict(os.environ, envmap, clear=True):
            env = tools._child_env()
        for keep in ("PATH", "JAVA_HOME", "LC_ALL", "GIT_AUTHOR_NAME"):
            self.assertIn(keep, env, keep)
        for drop in ("DATABASE_URL", "GH_TOKEN", "SOME_RANDOM_VAR", "TWOB_SEATBELT"):
            self.assertNotIn(drop, env, drop)

    def test_opt_out_inherits_full_env(self):
        with mock.patch.dict(os.environ, {"TWOB_NO_ENV_SCRUB": "1"}, clear=False):
            self.assertIsNone(tools._child_env())

    def test_file_path_vars_are_kept(self):
        # *_FILE / *_PATH name a path to a credential, not the secret — keep them.
        envmap = {"PATH": "/b", "AWS_WEB_IDENTITY_TOKEN_FILE": "/tok", "VAULT_TOKEN": "s",
                  "SECRET_KEY_PATH": "/p"}
        with mock.patch.dict(os.environ, envmap, clear=True):
            env = tools._child_env()
        self.assertIn("AWS_WEB_IDENTITY_TOKEN_FILE", env)
        self.assertIn("SECRET_KEY_PATH", env)
        self.assertNotIn("VAULT_TOKEN", env)   # actual secret value → dropped


class BoundedOutput(unittest.TestCase):
    def test_finite_large_output_is_capped(self):
        rc, out, status = tools._run_cancellable(
            [sys.executable, "-c", "import sys; sys.stdout.write('a'*3000000)"],
            shell=False, timeout=30, cancel=None)
        self.assertEqual(status, "ok")
        self.assertIn("output truncated", out)
        self.assertLess(len(out), 2_200_000)

    def test_infinite_output_is_stopped(self):
        rc, out, status = tools._run_cancellable(
            [sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('a'*8192)"],
            shell=False, timeout=30, cancel=None)
        self.assertIn("output truncated", out)
        self.assertLess(len(out), 2_200_000)   # stopped near the cap, not run to timeout

    def test_kill_failed_does_not_hang(self):
        # Regression: when _killpg can't stop the child (root/sudo/D-state), the call
        # must still return promptly with 'kill_failed' — not block on stdout.close()
        # until the child exits. The reader thread owns/closes the fd for this reason.
        with mock.patch.object(tools, "_killpg", return_value=False):
            cancel = threading.Event()
            timer = threading.Timer(0.3, cancel.set)
            timer.start()
            start = time.monotonic()
            rc, out, status = tools._run_cancellable(
                [sys.executable, "-c", "import time; time.sleep(3)"],
                shell=False, timeout=30, cancel=cancel)
            elapsed = time.monotonic() - start
            timer.cancel()
        self.assertEqual(status, "kill_failed")
        self.assertLess(elapsed, 2.0, "kill_failed must return promptly, not wait out the child")

    def test_env_reaches_child(self):
        # a scrubbed env actually applies: the secret isn't visible to the child
        with mock.patch.dict(os.environ, {"PATH": os.environ["PATH"], "MY_SECRET_TOKEN": "leak"}, clear=True):
            env = tools._child_env()
            rc, out, status = tools._run_cancellable(
                [sys.executable, "-c", "import os; print(os.environ.get('MY_SECRET_TOKEN', 'ABSENT'))"],
                shell=False, timeout=30, cancel=None, env=env)
        self.assertEqual(status, "ok")
        self.assertIn("ABSENT", out)


if __name__ == "__main__":
    unittest.main()
