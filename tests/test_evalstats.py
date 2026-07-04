"""Tests for P9 eval-rigor primitives: the exact-match scorer, seeded bootstrap CIs, the
McNemar paired test, across-seed variance, and the publish guard. All pure/deterministic.
Run: `python -m unittest tests.test_evalstats`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import evalstats as es  # noqa: E402


class ExactMatch(unittest.TestCase):
    def test_scalar_punctuation_and_space_insensitive(self):
        self.assertTrue(es.exact_match("Yes.", "yes"))
        self.assertTrue(es.exact_match("  Hello World ", "helloworld"))
        self.assertFalse(es.exact_match("cat", "dog"))

    def test_strips_currency_percent_thousands(self):
        self.assertTrue(es.exact_match("$1,000", "1000"))
        self.assertTrue(es.exact_match("42%", "42"))

    def test_listlike_multiset_with_length_check(self):
        self.assertTrue(es.exact_match("apple, banana", "banana; apple"))   # order-insensitive
        self.assertFalse(es.exact_match("apple, banana", "apple"))          # length mismatch
        self.assertFalse(es.exact_match("a, b, c", "a, b, x"))              # element mismatch

    def test_compact_numeric_lists_are_not_collapsed_by_thousands_strip(self):
        # A grouping comma is exactly 3-wide; a comma with 1-2 trailing digits is a list sep.
        self.assertFalse(es.exact_match("1,23", "12,3"))     # different 2-item lists
        self.assertTrue(es.exact_match("1,2,3", "3,2,1"))    # same multiset, any order
        self.assertFalse(es.exact_match("10,20", "1,020"))   # 2-item list vs one thousands number
        self.assertTrue(es.exact_match("1,000,2,000", "2000, 1000"))   # list of two grouped numbers

    def test_empty_elements_count_toward_length(self):
        self.assertFalse(es.exact_match("a,,b", "a,b"))      # 3 slots vs 2 — a real length mismatch

    def test_scalar_vs_list_are_not_conflated(self):
        # An expected scalar with no separator compares as a scalar.
        self.assertTrue(es.exact_match("hello world", "Hello, World"))      # got's comma is punctuation here
        self.assertFalse(es.exact_match("a; b", "ab"))                      # expected is a 2-elem list


class Bootstrap(unittest.TestCase):
    def test_reproducible_with_seed(self):
        vals = [1, 0, 1, 1, 0, 1, 0, 1]
        self.assertEqual(es.bootstrap_ci(vals, seed=7), es.bootstrap_ci(vals, seed=7))

    def test_all_ones_ci_is_at_one(self):
        lo, hi = es.bootstrap_ci([1, 1, 1, 1], seed=0)
        self.assertEqual((lo, hi), (1.0, 1.0))

    def test_single_value_is_point_interval(self):
        self.assertEqual(es.bootstrap_ci([0.5]), (0.5, 0.5))

    def test_empty_is_zero(self):
        self.assertEqual(es.bootstrap_ci([]), (0.0, 0.0))

    def test_interval_brackets_the_mean(self):
        vals = [1, 1, 1, 0, 0]
        lo, hi = es.bootstrap_ci(vals, seed=1)
        self.assertLessEqual(lo, 0.6)
        self.assertGreaterEqual(hi, 0.6)


class McNemar(unittest.TestCase):
    def test_no_discordant_pairs_is_p1(self):
        r = es.mcnemar([(True, True), (False, False), (True, True)])
        self.assertEqual(r["p_value"], 1.0)
        self.assertEqual((r["b"], r["c"]), (0, 0))

    def test_all_one_direction_counts(self):
        # base right, ablation wrong in every discordant pair.
        r = es.mcnemar([(True, False)] * 8)
        self.assertEqual((r["b"], r["c"], r["n"]), (8, 0, 8))
        self.assertLess(r["p_value"], 0.05)      # a strong, significant difference

    def test_symmetric_split_is_not_significant(self):
        r = es.mcnemar([(True, False), (False, True)])
        self.assertGreater(r["p_value"], 0.05)


class SeedSummary(unittest.TestCase):
    def test_single_seed_has_zero_variance(self):
        s = es.seed_summary([0.5])
        self.assertEqual(s["variance"], 0.0)
        self.assertEqual(s["n"], 1)

    def test_multi_seed_reports_spread(self):
        s = es.seed_summary([1.0, 0.0, 0.5])
        self.assertEqual(s["n"], 3)
        self.assertGreater(s["stdev"], 0.0)
        self.assertAlmostEqual(s["mean"], 0.5, places=3)


class PublishGuard(unittest.TestCase):
    def test_dirty_tree_refused(self):
        ok, reason = es.can_publish({"git_sha": "abc", "dirty_files": 3})
        self.assertFalse(ok)
        self.assertIn("dirty", reason)

    def test_clean_tree_ok(self):
        ok, _ = es.can_publish({"git_sha": "abc123def456", "dirty_files": 0})
        self.assertTrue(ok)

    def test_unknown_state_refused(self):
        ok, reason = es.can_publish({"git_sha": None, "dirty_files": None})
        self.assertFalse(ok)
        self.assertIn("unknown", reason)

    def test_snapshot_carries_sampling(self):
        snap = es.env_snapshot({"temperature": 0.2, "seeds": [0, 1, 2]})
        self.assertEqual(snap["sampling"]["temperature"], 0.2)
        self.assertIn("git_sha", snap)
        self.assertIn("dirty_files", snap)


if __name__ == "__main__":
    unittest.main()
