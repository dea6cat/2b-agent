"""Tests for src/two_b/web.py — stdlib fetch + readable extraction. Host-side, no network
(fetch is monkeypatched). Run: `python -m unittest tests.test_web`.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import web  # noqa: E402

_PAGE = """
<html><head><title>T</title><style>.x{color:red}</style>
<script>evil()</script></head>
<body>
  <nav>Home About Contact</nav>
  <main>
    <h1>Title Here</h1>
    <p>First   paragraph with <a href="https://ex.com/x">a link</a> inside.</p>
    <div hidden>SECRET HIDDEN TEXT</div>
    <div aria-hidden="true">ALSO HIDDEN</div>
    <ul><li>alpha</li><li>beta</li></ul>
    <script>tracker()</script>
  </main>
  <footer>© 2026</footer>
</body></html>
"""


class Extract(unittest.TestCase):
    def test_drops_noise_and_hidden(self):
        out = web.extract_readable(_PAGE)
        for gone in ("evil()", "tracker()", "color:red", "SECRET HIDDEN", "ALSO HIDDEN",
                     "Home About", "© 2026"):
            self.assertNotIn(gone, out, gone)
        self.assertIn("Title Here", out)
        self.assertIn("alpha", out)
        self.assertIn("beta", out)

    def test_prefers_main_over_body(self):
        # nav/footer live outside <main>; main-preference means they're excluded entirely
        out = web.extract_readable(_PAGE)
        self.assertNotIn("Contact", out)
        self.assertTrue(out.startswith("Title Here"))

    def test_collapses_whitespace(self):
        self.assertIn("First paragraph with", web.extract_readable(_PAGE))  # runs collapsed

    def test_markdown_headings_lists_links(self):
        md = web.extract_readable(_PAGE, as_markdown=True)
        self.assertIn("# Title Here", md)
        self.assertIn("[a link](https://ex.com/x)", md)
        self.assertIn("- alpha", md)

    def test_max_chars_caps_on_line_boundary(self):
        big = "<body><main>" + "".join(f"<p>line {i}</p>" for i in range(500)) + "</main></body>"
        out = web.extract_readable(big, max_chars=200)
        self.assertLessEqual(len(out), 260)   # cap + truncation marker
        self.assertIn("content truncated", out)

    def test_never_raises_on_junk(self):
        for junk in ("", "not html", "<p>unclosed <b>bold <a href=/y>lnk",
                     "<<<>>>", "<div><div><div>", "<a href=x>", "<!-- only a comment -->"):
            self.assertIsInstance(web.extract_readable(junk, as_markdown=True), str)

    def test_unclosed_anchor_text_not_lost(self):
        self.assertIn("lnk", web.extract_readable("<a href=/y>lnk", as_markdown=True))

    def test_hostile_deep_nesting_is_bounded(self):
        # a pathological page can't blow up output
        out = web.extract_readable("<div>" * 20000 + "x" + "</div>" * 20000, max_chars=1000)
        self.assertLessEqual(len(out), 1100)

    def test_trailing_text_delivered_by_close(self):
        # convert_charrefs buffers trailing text until a boundary; close() must flush it,
        # else a page truncated by the byte cap loses its tail. (Not the unclosed-CDATA case.)
        self.assertIn("Trailing text", web.extract_readable("<main><p>Trailing text no closing tag"))

    def test_unclosed_script_is_bounded_not_crashing(self):
        # documented limit: text inside an unclosed <script> is CDATA and may be dropped —
        # assert only that it never raises and stays a str (browsers behave the same way)
        self.assertIsInstance(web.extract_readable("<body><script>x=1;<p>after</p>"), str)

    def test_mismatched_tags_bounded_and_prompt(self):
        import time
        bomb = "<x>" * 50000 + "</y>" * 50000     # opens never closed + closes never opened
        t = time.monotonic()
        out = web.extract_readable(bomb, max_chars=500)
        self.assertLess(time.monotonic() - t, 5.0, "must not go quadratic")
        self.assertLessEqual(len(out), 600)

    def test_anchor_buffer_not_quadratic(self):
        import time
        # a single long-lived <a> with many nested inline children (reachable via /fetch's
        # as_markdown path) must stay linear, not O(n²) from re-summing the buffer
        page = "<main><a href=x>" + "".join(f"<i>w{i}</i>" for i in range(20000)) + "</a></main>"
        t = time.monotonic()
        web.extract_readable(page, as_markdown=True, max_chars=1000)
        self.assertLess(time.monotonic() - t, 5.0, "anchor buffer must not go quadratic")

    def test_crossing_skip_tags_do_not_wedge(self):
        # differently-named skip/hidden tags closing in reversed order must NOT permanently
        # stick skip-state on and swallow the rest of the page
        out = web.extract_readable(
            "<body><div hidden><span hidden>SECRET</div></span><p>Real content</p></body>")
        self.assertIn("Real content", out)
        self.assertNotIn("SECRET", out)

    def test_normal_tag_nested_in_skip_region_stays_skipped(self):
        # a same-named non-skip tag inside a hidden region must not prematurely end the skip
        out = web.extract_readable(
            "<body><div hidden>SKIPME<div>INNER</div>ALSOSKIP</div><p>keep this</p></body>")
        self.assertIn("keep this", out)
        for gone in ("SKIPME", "INNER", "ALSOSKIP"):
            self.assertNotIn(gone, out)

    def test_skip_and_hidden_filter_hold_at_any_depth(self):
        # regression: skip/hidden filtering must not lapse past a nesting-depth cap
        deep = "<main>" + "<div>" * 600 + "<script>LEAKSCRIPT</script><div hidden>LEAKHIDDEN</div>" \
               + "<p>real content</p>" + "</div>" * 600 + "</main>"
        out = web.extract_readable(deep)
        self.assertNotIn("LEAKSCRIPT", out)
        self.assertNotIn("LEAKHIDDEN", out)
        self.assertIn("real content", out)

    def test_giant_single_text_node_capped(self):
        out = web.extract_readable("<main>" + "x" * 5_000_000, max_chars=1000)
        self.assertLessEqual(len(out), 1100)


class SSRF(unittest.TestCase):
    def test_private_and_loopback_hosts_refused(self):
        for host in ("127.0.0.1", "localhost", "10.0.0.1", "169.254.169.254", "192.168.1.1"):
            self.assertFalse(web._host_is_public(host), host)

    def test_ok_url_requires_https_and_public(self):
        self.assertFalse(web._ok_url("http://example.com"))       # not https
        self.assertFalse(web._ok_url("https://127.0.0.1/"))       # not public
        self.assertFalse(web._ok_url("ftp://x"))

    def test_fetch_refuses_internal(self):
        self.assertIsNone(web.fetch("https://127.0.0.1/x"))       # blocked before any network

    def test_redirect_handler_refuses_downgrade_and_internal(self):
        h = web._SafeRedirect()
        self.assertIsNone(h.redirect_request(None, None, 302, "m", {}, "http://evil.example"))
        self.assertIsNone(h.redirect_request(None, None, 302, "m", {}, "https://127.0.0.1/"))


class Fetch(unittest.TestCase):
    def test_refuses_non_https(self):
        self.assertIsNone(web.fetch("ftp://example.com"))
        self.assertIsNone(web.fetch("file:///etc/passwd"))

    def test_returns_none_on_error(self):
        # fetch uses build_opener(...).open — patch that (not the unused urlopen) so the
        # error path is exercised hermetically, never touching the real network.
        with mock.patch.object(web, "_host_is_public", return_value=True), \
             mock.patch("urllib.request.OpenerDirector.open", side_effect=OSError("boom")):
            self.assertIsNone(web.fetch("https://example.com"))

    def test_decodes_body(self):
        class _R:
            headers = type("H", (), {"get_content_charset": staticmethod(lambda: "utf-8")})()
            def read(self, n): return b"<body><main>hi there</main></body>"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        # fetch uses build_opener(...).open now, and checks the host is public first
        with mock.patch.object(web, "_host_is_public", return_value=True), \
             mock.patch("urllib.request.OpenerDirector.open", return_value=_R()):
            html = web.fetch("https://example.com")
        self.assertIn("hi there", web.extract_readable(html))


class FetchCommand(unittest.TestCase):
    """/fetch is a host-side USER command that injects a page into task context — it must
    NOT add a model-facing tool (frozen schema)."""

    def _app(self):
        import types
        from two_b.session import Task

        class _UI:
            def __init__(self): self.msgs = []
            def print(self, m): self.msgs.append(str(m))

        task = Task(description="x")
        app = types.SimpleNamespace(session=types.SimpleNamespace(active_task=task, tasks=[task]), ui=_UI())
        return app, task

    def test_registered_but_not_a_model_tool(self):
        from two_b import commands
        self.assertIn("fetch", commands.COMMANDS)                 # a slash command
        from two_b.toolspec import TOOL_SPECS
        self.assertNotIn("fetch", {s.name for s in TOOL_SPECS})   # NOT a model tool

    def test_injects_readable_content(self):
        from two_b import commands, web
        app, task = self._app()
        page = "<body><nav>nope</nav><main><h1>Hi</h1><p>Body text here</p></main></body>"
        with mock.patch.object(web, "fetch", return_value=page):
            commands.COMMANDS["fetch"]("https://example.com/doc", app)
        self.assertIsNotNone(task.conversation)
        blob = "\n".join(m.text or "" for m in task.conversation.messages)
        self.assertIn("Body text here", blob)
        self.assertIn("pre-loaded web page: https://example.com/doc", blob)
        self.assertNotIn("nope", blob)                            # nav dropped
        # fetched page is fenced as untrusted (external content = injection vector)
        self.assertIn("<untrusted_data", blob)
        self.assertIn("from=web:https://example.com/doc", blob)

    def test_fetch_failure_is_clean(self):
        from two_b import commands, web
        app, task = self._app()
        with mock.patch.object(web, "fetch", return_value=None):
            commands.COMMANDS["fetch"]("https://bad", app)
        self.assertIsNone(task.conversation)                      # nothing injected
        self.assertTrue(any("Could not fetch" in m for m in app.ui.msgs))


if __name__ == "__main__":
    unittest.main()
