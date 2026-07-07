"""Keyboard scrolling for the conversation log (PageUp/PageDown). Terminal.app only forwards
the mouse wheel in the any-event mode 2B disables to stop the flood, so scrolling must not
depend on the wheel. Guarded on textual (runtime-only dep).
Run: `python -m unittest tests.test_log_scroll`.
"""
import asyncio
import os
import sys
import unittest

os.environ.setdefault("TEXTUAL_ANIMATIONS", "none")   # instant scroll -> deterministic offsets
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from rich.text import Text
    from textual.containers import VerticalScroll
    from two_b.app_tui import TwoBApp
    _HAS_TEXTUAL = True
except ModuleNotFoundError:
    _HAS_TEXTUAL = False


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed (runtime-only dependency)")
class LogKeyboardScroll(unittest.IsolatedAsyncioTestCase):
    async def test_pageup_pagedown_scroll_the_log(self):
        app = TwoBApp(model="fake:m", auto_yes=True, initial_task=None)
        async with app.run_test() as pilot:
            await pilot.pause()                      # let compose settle before querying
            log = app.query_one("#log", VerticalScroll)
            for i in range(120):                     # far taller than the test viewport
                app.log_write(Text(f"line {i}"))
            await pilot.pause()

            at_bottom = log.scroll_offset.y          # log auto-scrolled to the newest line
            self.assertGreater(at_bottom, 0)         # there is scrollable content

            await pilot.press("pageup")
            await pilot.pause()
            scrolled_up = log.scroll_offset.y
            self.assertLess(scrolled_up, at_bottom)  # PageUp moved the view up

            await pilot.press("pagedown")
            await pilot.pause()
            self.assertGreater(log.scroll_offset.y, scrolled_up)   # PageDown moved it back down

    async def test_shift_arrows_scroll_the_log(self):
        app = TwoBApp(model="fake:m", auto_yes=True, initial_task=None)
        async with app.run_test() as pilot:
            await pilot.pause()
            log = app.query_one("#log", VerticalScroll)
            for i in range(120):
                app.log_write(Text(f"line {i}"))
            await pilot.pause()

            at_bottom = log.scroll_offset.y
            await pilot.press("shift+up")
            await pilot.pause()
            up = log.scroll_offset.y
            self.assertLess(up, at_bottom)               # shift+↑ scrolled up (a line)
            await pilot.press("shift+down")
            await pilot.pause()
            self.assertGreater(log.scroll_offset.y, up)  # shift+↓ scrolled back down


if __name__ == "__main__":
    unittest.main()
