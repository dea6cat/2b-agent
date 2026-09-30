"""Tests for P9 eval-rigor primitives: seeded bootstrap CIs, the
McNemar paired test, across-seed variance, and the publish guard. All pure/deterministic.
Run: `python -m unittest tests.test_evalstats`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.evals import evalstats as es  # noqa: E402


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
