"""Regression: a burst of terminal mouse-motion reports (SGR sequences like
"\\x1b[<35;77;29M") arrives as a Paste and TextArea inserts it verbatim, flooding the
input box with garbage. The task input must strip terminal escape sequences (and stray
control chars) from pasted text while preserving real text and newlines.
Run: `python -m unittest tests.test_input_sanitize`.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from textual import events
    from two_b.app_tui import TwoBApp, TaskInput, _sanitize_pasted
    _HAS_TEXTUAL = True
except ModuleNotFoundError:
    _HAS_TEXTUAL = False


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed (runtime-only dependency)")
class SanitizePasted(unittest.TestCase):
    def test_sgr_mouse_sequences_are_stripped(self):
        blob = "\x1b[<35;77;29M\x1b[<35;76;29M\x1b[<35;73;29M"
        self.assertEqual(_sanitize_pasted(blob), "")

    def test_mouse_burst_around_real_text_keeps_only_text(self):
        self.assertEqual(_sanitize_pasted("\x1b[<35;77;29Mhello\x1b[<35;76;29M"), "hello")

    def test_plain_text_and_newlines_survive(self):
        self.assertEqual(_sanitize_pasted("line one\nline two\ttabbed"), "line one\nline two\ttabbed")

    def test_control_chars_removed_text_kept(self):
        # NUL and BEL are stripped; the surrounding letters survive.
        self.assertEqual(_sanitize_pasted("a\x00b\x07c"), "abc")


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed (runtime-only dependency)")
class PasteIntoInput(unittest.IsolatedAsyncioTestCase):
    async def test_paste_of_mouse_garbage_does_not_corrupt_input(self):
        app = TwoBApp(model="fake:m", auto_yes=True, initial_task=None)
        async with app.run_test():
            inp = app.query_one("#input", TaskInput)
            inp.focus()
            await inp._on_paste(events.Paste("\x1b[<35;77;29M\x1b[<35;76;29Mreview the diff"))
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertNotIn("\x1b", inp.text)
            self.assertNotIn("35;77;29M", inp.text)
            self.assertEqual(inp.text, "review the diff")

    async def test_normal_multiline_paste_still_works(self):
        app = TwoBApp(model="fake:m", auto_yes=True, initial_task=None)
        async with app.run_test():
            inp = app.query_one("#input", TaskInput)
            inp.focus()
            await inp._on_paste(events.Paste("first line\nsecond line"))
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(inp.text, "first line\nsecond line")


if __name__ == "__main__":
    unittest.main()
