"""Tests for src/two_b/discover.py — live-model discovery from ollama.com, parsed from a
checked-in HTML fixture (no network). Run: `python -m unittest tests.test_discover`.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import discover  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "ollama_search_tools.html")


def _html():
    return open(_FIXTURE, encoding="utf-8", errors="replace").read()


class Parse(unittest.TestCase):
    def test_parses_real_fixture(self):
        cands = discover.parse_search(_html())
        self.assertGreaterEqual(len(cands), 15)                    # ~20 models on the page
        by_slug = {c["slug"]: c for c in cands}
        # a model with discrete pullable sizes exposes them + its tools capability
        g = by_slug.get("granite4.1")
        self.assertIsNotNone(g)
        self.assertIn("tools", g["caps"])
        self.assertTrue(set(g["sizes"]) >= {3.0, 8.0})             # 3b/8b (+30b) offered
        self.assertGreater(g["pulls"], 0)

    def test_malformed_blocks_skipped_not_raised(self):
        self.assertEqual(discover.parse_search(""), [])
        self.assertEqual(discover.parse_search("<li x-test-model>no slug here</li>"), [])
        self.assertIsInstance(discover.parse_search("<garbage<<>>"), list)

    def test_trailing_content_after_card_not_attributed(self):
        # markup after a card's </li> must not be swept into that card's caps/sizes
        html = ('<li x-test-model><a href="/library/real"></a>'
                '<span x-test-capability>tools</span><span x-test-size>8b</span></li>'
                '<span x-test-capability>vision</span><span x-test-size>999b</span>')
        c = discover.parse_search(html)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["slug"], "real")
        self.assertEqual(c[0]["caps"], {"tools"})          # not "vision" from the trailing junk
        self.assertEqual(c[0]["sizes"], [8.0])             # not 999b

    def test_pulls_parsing(self):
        self.assertEqual(discover._pulls_to_int("16.9", "M"), 16_900_000)
        self.assertEqual(discover._pulls_to_int("223.8", "K"), 223_800)
        self.assertEqual(discover._pulls_to_int("3,673", ""), 3673)


class Fit(unittest.TestCase):
    def test_est_ram(self):
        self.assertEqual(discover.est_ram(8), 10)
        self.assertEqual(discover.est_ram(4), 6)
        self.assertEqual(discover.est_ram(0.6), 4)                 # floor at 4

    def test_fit_variant_picks_largest_fitting(self):
        self.assertEqual(discover.fit_variant([4, 8, 30], 10), 8)  # est_ram(8)=10 ≤ 10
        self.assertEqual(discover.fit_variant([4, 8, 30], 6), 4)
        self.assertIsNone(discover.fit_variant([30, 70], 8))       # nothing fits


class Discover(unittest.TestCase):
    def test_offline_env_returns_empty(self):
        with mock.patch.dict(os.environ, {"TWOB_NO_MODEL_FETCH": "1"}):
            self.assertEqual(discover.discover(64), [])

    def test_fetch_failure_returns_empty(self):
        with mock.patch.object(discover.web, "fetch", return_value=None):
            self.assertEqual(discover.discover(64), [])

    def test_ranks_by_pulls_and_filters(self):
        with mock.patch.object(discover.web, "fetch", return_value=_html()):
            rows = discover.discover(64)
        self.assertTrue(rows)
        for tag, pulls, ram in rows:
            self.assertRegex(tag, r"^[a-z0-9][a-z0-9._-]*:\d+(?:\.\d+)?b$")   # slug:Nb
            self.assertLessEqual(ram, 64)
        pulls_seq = [p for _, p, _ in rows]
        self.assertEqual(pulls_seq, sorted(pulls_seq, reverse=True))          # popularity-ranked

    def test_coding_url_is_used_when_passed(self):
        seen = {}

        def fake_fetch(url, *args, **kwargs):
            seen["url"] = url
            return _html()
        with mock.patch.object(discover.web, "fetch", fake_fetch):
            discover.discover(64, discover.CODING_URL)
        self.assertEqual(seen["url"], discover.CODING_URL)
        self.assertIn("q=coding", discover.CODING_URL)

    def test_low_ram_models_are_subset_of_high_ram(self):
        # a model that fits at 6GB also fits at 64GB (possibly as a LARGER variant, so
        # compare model slugs, not exact tags)
        with mock.patch.object(discover.web, "fetch", return_value=_html()):
            small = {t.split(":")[0] for t, _, _ in discover.discover(6)}
            big = {t.split(":")[0] for t, _, _ in discover.discover(64)}
        self.assertTrue(small <= big)


if __name__ == "__main__":
    unittest.main()
