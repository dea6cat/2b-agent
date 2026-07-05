"""Tests for the run_command write-confinement sandbox (seatbelt).

Pure policy/argv/mode tests run everywhere; the behavioral tests that actually invoke
`/usr/bin/sandbox-exec` are gated on macOS. Host-side. Run:
`python -m unittest tests.test_seatbelt`.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import seatbelt, tools  # noqa: E402


class Pure(unittest.TestCase):
    def test_policy_structure(self):
        p = seatbelt.build_policy(2, 1)
        self.assertIn("(version 1)", p)
        self.assertIn("(allow default)", p)
        self.assertIn("(deny file-write*)", p)
        self.assertIn('(subpath (param "WRITABLE_ROOT_0"))', p)
        self.assertIn('(subpath (param "WRITABLE_ROOT_1"))', p)
        # protected denied as BOTH literal and subpath (uncreatable + unwritable)
        self.assertIn('(literal (param "PROTECTED_0"))', p)
        self.assertIn('(subpath (param "PROTECTED_0"))', p)
        self.assertNotIn("(deny network*)", p)

    def test_strict_adds_network_deny(self):
        self.assertIn("(deny network*)", seatbelt.build_policy(1, 1, strict=True))

    def test_strict_confines_reads(self):
        p = seatbelt.build_policy(2, 1, strict=True)
        self.assertIn("(deny file-read*)", p)
        self.assertIn('(subpath (param "WRITABLE_ROOT_0"))', p.split("(deny file-read*)")[1])  # roots readable
        self.assertIn('(subpath "/usr")', p)                                                   # system readable
        # default (non-strict) leaves reads open
        self.assertNotIn("(deny file-read*)", seatbelt.build_policy(2, 1))

    def test_argv_shape_and_param_passing(self):
        argv = seatbelt.build_argv("echo hi > f", ["/ws", "/tmp/x"], ["/ws/.git"])
        self.assertEqual(argv[0], seatbelt.SANDBOX_EXEC)   # hard-pinned, absolute
        self.assertEqual(argv[1], "-p")
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "echo hi > f"])
        self.assertIn("WRITABLE_ROOT_0=/ws", argv)
        self.assertIn("WRITABLE_ROOT_1=/tmp/x", argv)
        self.assertIn("PROTECTED_0=/ws/.git", argv)
        # paths travel ONLY via -D, never interpolated into the policy string
        policy = argv[2]
        self.assertNotIn("/ws", policy)
        self.assertNotIn("/tmp/x", policy)

    def test_relative_root_rejected(self):
        with self.assertRaises(ValueError):
            seatbelt.build_argv("x", ["relative/dir"], [])

    def test_roots_and_protected_are_absolute(self):
        roots = seatbelt.writable_roots()
        for r in roots:
            self.assertTrue(os.path.isabs(r))
        self.assertTrue(any(p.endswith(".git") for p in seatbelt.protected_paths()))
        # package-manager DOWNLOAD caches are writable so default-on doesn't break installs
        self.assertTrue(any(r.endswith("/.npm") for r in roots))
        self.assertTrue(any(r.endswith("/.cargo/registry") for r in roots))
        self.assertTrue(any(r.endswith("/.m2/repository") for r in roots))
        # ...but the package-manager ROOTS (init scripts / creds) are NOT writable
        self.assertFalse(any(r.endswith("/.gradle") or r.endswith("/.m2") or r.endswith("/.cargo")
                             for r in roots), "pkg-manager roots must not be writable")

    def test_looks_like_denial(self):
        self.assertFalse(seatbelt.looks_like_denial(0, "Operation not permitted"))
        self.assertFalse(seatbelt.looks_like_denial(1, "syntax error near token"))
        self.assertTrue(seatbelt.looks_like_denial(1, "sh: f: Operation not permitted"))
        self.assertTrue(seatbelt.looks_like_denial(1, "touch: /etc/x: Read-only file system"))  # bwrap

    def test_mode_env(self):
        with mock.patch.object(seatbelt, "is_available", return_value=True):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TWOB_SEATBELT", None)
                os.environ.pop("TWOB_NO_SEATBELT", None)
                self.assertEqual(seatbelt.mode(), "on")            # on by default
            with mock.patch.dict(os.environ, {"TWOB_SEATBELT": "strict"}):
                self.assertEqual(seatbelt.mode(), "strict")
            with mock.patch.dict(os.environ, {"TWOB_NO_SEATBELT": "1"}):
                self.assertEqual(seatbelt.mode(), "off")
        with mock.patch.object(seatbelt, "is_available", return_value=False):
            self.assertEqual(seatbelt.mode(), "off")               # unavailable ⇒ off

    def test_wrap_off_returns_none(self):
        with mock.patch.object(seatbelt, "mode", return_value="off"):
            self.assertEqual(seatbelt.wrap("echo x"), (None, False))


class DenyRerunWiring(unittest.TestCase):
    """do_run_command's deny→re-run logic, without a real sandbox."""

    def test_denial_without_callback_stands_with_hint(self):
        fake = ["sbx", "-p", "pol", "--", "/bin/sh", "-c", "echo x > /etc/y"]
        with mock.patch.object(tools.seatbelt, "wrap", return_value=(fake, False)), \
             mock.patch.object(tools, "_run_cancellable",
                               return_value=(1, "sh: Operation not permitted", "ok")):
            out = tools.do_run_command("echo x > /etc/y")   # on_denied=None ⇒ fail closed
        self.assertIn("sandbox blocked a write", out)

    def test_denial_with_callback_reruns_unsandboxed(self):
        fake = ["sbx", "-p", "pol", "--", "/bin/sh", "-c", "cmd"]
        calls = []

        def runner(cmd, *, shell, timeout, cancel, env=None):
            calls.append(shell)
            # First (sandboxed, shell=False) denial; re-run (shell=True) succeeds.
            return (1, "Operation not permitted", "ok") if not shell else (0, "done", "ok")

        with mock.patch.object(tools.seatbelt, "wrap", return_value=(fake, False)), \
             mock.patch.object(tools, "_run_cancellable", side_effect=runner):
            out = tools.do_run_command("cmd", on_denied=lambda: True)
        self.assertIn("done", out)               # output is fenced as untrusted
        self.assertEqual(calls, [False, True])   # sandboxed then unsandboxed re-run


class BwrapPure(unittest.TestCase):
    def test_argv_structure(self):
        argv = seatbelt.build_bwrap_argv("echo hi > f", ["/ws", "/tmp/x"], ["/ws/.git"], [], "/usr/bin/bwrap")
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "echo hi > f"])
        # whole fs read-only, then writable roots rw, then protected re-locked ro
        self.assertEqual(argv[1:4], ["--ro-bind", "/", "/"])
        self.assertIn("--die-with-parent", argv)
        joined = " ".join(argv)
        self.assertIn("--bind-try /ws /ws", joined)
        self.assertIn("--bind-try /tmp/x /tmp/x", joined)
        self.assertIn("--ro-bind-try /ws/.git /ws/.git", joined)
        # existing protected re-lock must come AFTER the writable-root binds
        self.assertLess(joined.index("--bind-try /ws /ws"), joined.index("--ro-bind-try /ws/.git"))
        self.assertNotIn("--unshare-net", argv)

    def test_missing_protected_uses_readonly_tmpfs(self):
        # a missing protected path (e.g. .git in a non-repo dir) is uncreatable AND writes
        # fail loudly (read-only tmpfs) rather than silently succeeding
        joined = " ".join(seatbelt.build_bwrap_argv("x", ["/ws"], [], ["/ws/.git"], "/usr/bin/bwrap"))
        self.assertIn("--tmpfs /ws/.git --remount-ro /ws/.git", joined)
        self.assertNotIn("--ro-bind-try /ws/.git", joined)

    def test_strict_unshares_net(self):
        argv = seatbelt.build_bwrap_argv("x", ["/ws"], [], [], "/usr/bin/bwrap", strict=True)
        self.assertIn("--unshare-net", argv)

    def test_strict_confines_reads(self):
        # strict binds only system read dirs (not all of /), so $HOME stays unreadable
        argv = seatbelt.build_bwrap_argv("x", ["/ws"], [], [], "/usr/bin/bwrap", strict=True)
        joined = " ".join(argv)
        self.assertNotIn("--ro-bind / /", joined)
        self.assertIn("--ro-bind-try /usr /usr", joined)
        self.assertIn("--bind-try /ws /ws", joined)          # workspace still writable
        # default (non-strict) binds the whole fs read-only
        self.assertIn("--ro-bind / /", " ".join(
            seatbelt.build_bwrap_argv("x", ["/ws"], [], [], "/usr/bin/bwrap")))

    def test_relative_root_rejected(self):
        with self.assertRaises(ValueError):
            seatbelt.build_bwrap_argv("x", ["rel/dir"], [], [], "/usr/bin/bwrap")

    def test_root_slash_never_writable(self):
        # degenerate cwd=="/" must not become a writable root (would defeat confinement)
        import os as _os
        cwd = _os.getcwd()
        try:
            _os.chdir("/")
            self.assertNotIn("/", seatbelt.writable_roots())
        finally:
            _os.chdir(cwd)


_CAN_SANDBOX = sys.platform == "darwin" and os.path.exists(seatbelt.SANDBOX_EXEC)
_CAN_BWRAP = sys.platform.startswith("linux") and seatbelt._bwrap_path() is not None


@unittest.skipUnless(_CAN_BWRAP, "requires Linux bubblewrap")
class LinuxBwrapBehavior(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.ws, ".git"))
        self._cwd = os.getcwd()
        os.chdir(self.ws)
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def _run(self, command, strict=False):
        roots = seatbelt.writable_roots()
        seatbelt._ensure_writable_roots_exist(roots)
        prot = seatbelt.protected_paths()
        ro = [p for p in prot if os.path.exists(p)]
        tm = [p for p in prot if not os.path.exists(p)]
        argv = seatbelt.build_bwrap_argv(command, roots, ro, tm, seatbelt._bwrap_path(), strict=strict)
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)

    def test_write_inside_succeeds(self):
        r = self._run("echo hi > inside.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.ws, "inside.txt")))

    def test_write_outside_denied(self):
        r = self._run("echo hi > /etc/2b_bwrap_probe")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists("/etc/2b_bwrap_probe"))

    def test_write_into_existing_git_denied(self):
        # .git exists (created in setUp) → ro-bind → the real file is never written
        self._run("echo hi > .git/probe")
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".git", "probe")))

    def test_cannot_create_git_in_non_repo(self):
        # .git absent → tmpfs → a write "succeeds" into the throwaway mount but nothing
        # lands on the real fs (no planted backdoor)
        shutil.rmtree(os.path.join(self.ws, ".git"))
        self._run("mkdir -p .git/hooks && echo evil > .git/hooks/pre-commit")
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".git", "hooks", "pre-commit")))

    def test_read_outside_allowed(self):
        self.assertEqual(self._run("cat /etc/hostname >/dev/null").returncode, 0)


@unittest.skipUnless(_CAN_SANDBOX, "requires macOS sandbox-exec")
class DarwinBehavior(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.ws, ".git"))          # protected dir must exist to target it
        self._cwd = os.getcwd()
        os.chdir(self.ws)
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def _run(self, command, strict=False):
        argv = seatbelt.build_argv(command, seatbelt.writable_roots(),
                                   seatbelt.protected_paths(), strict=strict)
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)

    def test_write_inside_workspace_succeeds(self):
        r = self._run("echo hi > inside.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.ws, "inside.txt")))

    def test_write_outside_workspace_denied(self):
        probe = "/etc/2b_seatbelt_probe"
        r = self._run(f"echo hi > {probe}")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(probe))

    def test_write_into_git_denied(self):
        r = self._run("echo hi > .git/probe")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".git", "probe")))

    def test_read_outside_workspace_allowed(self):
        r = self._run("cat /etc/hosts >/dev/null")     # reads are not confined (write-only)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_strict_denies_network(self):
        # Under strict, the socket attempt is refused (EPERM) rather than routed.
        code = "import socket; socket.create_connection(('10.255.255.1', 80), timeout=2)"
        r = self._run(f"{sys.executable} -c {code!r}", strict=True)
        self.assertNotEqual(r.returncode, 0)


@unittest.skipUnless(_CAN_SANDBOX, "requires macOS sandbox-exec")
class DarwinReadConfine(unittest.TestCase):
    """strict mode confines reads: workspace + system readable, $HOME (secrets) not."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        with open(os.path.join(self.ws, "proj.txt"), "w") as f:
            f.write("project content\n")
        # a secret placed under $HOME (outside the readable roots + system allowlist)
        self.home_dir = tempfile.mkdtemp(dir=os.path.expanduser("~"))
        self.secret = os.path.join(self.home_dir, "secret.txt")
        with open(self.secret, "w") as f:
            f.write("TOP SECRET\n")
        self._cwd = os.getcwd()
        os.chdir(self.ws)
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.home_dir, ignore_errors=True)

    def _run(self, command):
        argv = seatbelt.build_argv(command, seatbelt.writable_roots(), seatbelt.protected_paths(), strict=True)
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)

    def test_home_secret_read_denied(self):
        r = self._run(f"cat {self.secret}")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("TOP SECRET", r.stdout)

    def test_workspace_read_allowed(self):
        r = self._run("cat proj.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("project content", r.stdout)

    def test_ordinary_command_still_runs(self):
        r = self._run("echo hi")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hi", r.stdout)

    def test_write_outside_still_denied(self):
        self.assertNotEqual(self._run("echo x > /etc/2b_strict_probe").returncode, 0)
        self.assertFalse(os.path.exists("/etc/2b_strict_probe"))


if __name__ == "__main__":
    unittest.main()
