"""Tests for esc-immediate-kill: cancellable subprocesses (kill the whole process
group on esc) and the LSP/MCP helper teardown.

Run: `python -m unittest tests.test_esc_kill` from the repo root. These shell out
to `sh`/`sleep`, so they run on any POSIX box (2B is macOS-first).
"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator, tools  # noqa: E402


class Cancellable(unittest.TestCase):
    def _marker(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, "marker")

    def test_normal_command_still_returns_output(self):
        self.assertIn("hello", tools.do_run_command("echo hello"))   # output is fenced as untrusted

    def test_nonzero_exit_is_flagged(self):
        out = tools.do_run_command("sh -c 'exit 3'")
        self.assertTrue(out.startswith("error: command exited 3"), out)

    def test_cancel_kills_the_running_process(self):
        marker = self._marker()
        cancel = threading.Event()
        # The command would create the marker after 2s; esc fires at 0.2s.
        threading.Timer(0.2, cancel.set).start()
        start = time.monotonic()
        out = tools.do_run_command(f"sleep 2; touch {marker}", cancel=cancel)
        elapsed = time.monotonic() - start
        self.assertTrue(out.startswith("stopped:"), out)
        self.assertLess(elapsed, 1.5, "cancel should return in ~0.1s, not wait out the sleep")
        time.sleep(2.2)  # give the killed sleep time to have fired, had it survived
        self.assertFalse(os.path.exists(marker), "process kept running after cancel")

    def test_cancel_kills_child_processes_in_the_group(self):
        child = self._marker()
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        # A backgrounded subshell child — it dies only if the whole group is killed.
        tools.do_run_command(f"(sleep 2; touch {child}) & sleep 5", cancel=cancel)
        time.sleep(2.4)
        self.assertFalse(os.path.exists(child), "a child in the process group survived esc")

    def test_timeout_still_enforced(self):
        start = time.monotonic()
        rc, out, status = tools._run_cancellable("sleep 5", shell=True, timeout=0.3, cancel=None)
        self.assertEqual(status, "timeout")
        self.assertLess(time.monotonic() - start, 2.0)

    def test_timeout_kills_children_in_the_group(self):
        child = self._marker()
        tools._run_cancellable(f"(sleep 2; touch {child}) & sleep 5", shell=True, timeout=0.3, cancel=None)
        time.sleep(2.4)
        self.assertFalse(os.path.exists(child), "a child survived the timeout kill")

    def test_kill_failure_is_reported_honestly(self):
        # Simulate a group we can't signal (e.g. a sudo'd child): really kill the
        # process so the test leaks nothing, but report the kill as failed.
        real = tools._killpg
        self.addCleanup(setattr, tools, "_killpg", real)
        tools._killpg = lambda proc: (real(proc), False)[1]
        cancel = threading.Event()
        threading.Timer(0.1, cancel.set).start()
        out = tools.do_run_command("sleep 3", cancel=cancel)
        self.assertIn("may still be running", out)
        self.assertFalse(out.startswith("stopped:"), "must not claim it stopped when the kill failed")

    def test_already_set_cancel_returns_immediately(self):
        cancel = threading.Event()
        cancel.set()
        start = time.monotonic()
        out = tools.do_run_command("sleep 5", cancel=cancel)
        self.assertTrue(out.startswith("stopped:"), out)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_git_runs_and_can_be_cancelled(self):
        # A real, fast git call still works end-to-end...
        self.assertIn("git version", tools.do_run_git("--version"))
        # ...and the cancel path maps to a quiet 'stopped', not an error.
        cancel = threading.Event()
        cancel.set()
        out = tools.do_run_git("log", cancel=cancel)
        self.assertTrue(out.startswith("stopped:"), out)


class Teardown(unittest.TestCase):
    def test_teardown_helpers_shuts_lsp_and_restarts_mcp(self):
        from two_b import lsp, mcp_client
        calls = []
        self.addCleanup(setattr, lsp, "shutdown_all", lsp.shutdown_all)
        lsp.shutdown_all = lambda: calls.append("lsp.shutdown_all")

        class FakeManager:
            def shutdown(self):
                calls.append("mcp.shutdown")

            def start(self):
                calls.append("mcp.start")

        self.addCleanup(setattr, mcp_client, "manager", mcp_client.manager)
        mcp_client.manager = FakeManager()

        orchestrator.teardown_helpers()
        self.assertEqual(calls, ["lsp.shutdown_all", "mcp.shutdown", "mcp.start"])

    def test_teardown_helpers_swallows_failures(self):
        from two_b import lsp, mcp_client
        self.addCleanup(setattr, lsp, "shutdown_all", lsp.shutdown_all)

        def boom():
            raise RuntimeError("lsp is wedged")

        lsp.shutdown_all = boom

        class FakeManager:
            def shutdown(self):
                raise RuntimeError("mcp is wedged")

            def start(self):
                pass

        self.addCleanup(setattr, mcp_client, "manager", mcp_client.manager)
        mcp_client.manager = FakeManager()
        orchestrator.teardown_helpers()  # must not raise


if __name__ == "__main__":
    unittest.main()
