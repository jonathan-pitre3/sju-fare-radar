"""Unit tests for baseline math, tier gating, and alert cooldowns.

Run: .venv/bin/python -m unittest discover tests
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fare_radar"))

import baselines
import store


def seed(conn, prices, origin="SJU", dest="MAD", trip_type="round_trip",
         start=date(2026, 6, 1), spacing_days=1, source_job="watch"):
    """Insert one observation per price, spaced spacing_days apart."""
    for i, price in enumerate(prices):
        at = datetime(start.year, start.month, start.day,
                      12, tzinfo=timezone.utc) + timedelta(days=i * spacing_days)
        store.record_observation(conn, {
            "observed_at": at.isoformat(timespec="seconds"),
            "origin": origin, "destination": dest, "trip_type": trip_type,
            "depart_date": "2026-09-01", "return_date": "2026-09-08",
            "price": float(price), "currency": "USD", "source_job": source_job,
        })


class PercentileTests(unittest.TestCase):
    def test_single_value(self):
        self.assertEqual(baselines.percentile([100.0], 10), 100.0)

    def test_median_odd_and_even(self):
        self.assertEqual(baselines.percentile([1, 2, 3], 50), 2)
        self.assertEqual(baselines.percentile([1, 2, 3, 4], 50), 2.5)

    def test_interpolation(self):
        # 0..100 in steps of 10: p25 lands exactly between 20 and 30
        vals = list(range(0, 101, 10))
        self.assertEqual(baselines.percentile(vals, 25), 25.0)
        self.assertEqual(baselines.percentile(vals, 10), 10.0)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            baselines.percentile([], 50)


class RouteStatsTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.today = date(2026, 7, 13)

    def test_no_data(self):
        s = baselines.route_stats(self.conn, "SJU", "MAD", "round_trip",
                                  today=self.today)
        self.assertEqual(s["n_observations"], 0)
        self.assertFalse(s["ready"])
        self.assertIsNone(s["median"])

    def test_not_ready_below_min_observations(self):
        seed(self.conn, [500] * 19, spacing_days=2)  # 19 obs over 36 days
        s = baselines.route_stats(self.conn, "SJU", "MAD", "round_trip",
                                  today=self.today)
        self.assertEqual(s["n_observations"], 19)
        self.assertFalse(s["ready"])

    def test_not_ready_below_min_span(self):
        seed(self.conn, [500] * 40, spacing_days=0)  # 40 obs, same day
        s = baselines.route_stats(self.conn, "SJU", "MAD", "round_trip",
                                  today=self.today)
        self.assertFalse(s["ready"])

    def test_ready_at_thresholds(self):
        seed(self.conn, [500] * 20, start=date(2026, 6, 1), spacing_days=1)
        # 20 obs spanning 19 days -> not ready; extend to 21-day span
        s = baselines.route_stats(self.conn, "SJU", "MAD", "round_trip",
                                  today=self.today)
        self.assertFalse(s["ready"])
        seed(self.conn, [500, 500], start=date(2026, 6, 21), spacing_days=1)
        s = baselines.route_stats(self.conn, "SJU", "MAD", "round_trip",
                                  today=self.today)
        self.assertTrue(s["ready"])

    def test_window_excludes_old_observations(self):
        seed(self.conn, [100] * 30, start=date(2025, 12, 1))   # > 120d ago
        seed(self.conn, [600] * 25, start=date(2026, 6, 1))
        s = baselines.route_stats(self.conn, "SJU", "MAD", "round_trip",
                                  today=self.today)
        self.assertEqual(s["n_observations"], 25)
        self.assertEqual(s["median"], 600)

    def test_groups_by_route_not_depart_date(self):
        seed(self.conn, [500] * 25, dest="MAD")
        seed(self.conn, [900] * 25, dest="FCO")
        s = baselines.route_stats(self.conn, "SJU", "MAD", "round_trip",
                                  today=self.today)
        self.assertEqual(s["median"], 500)

    def test_trip_type_separates_baselines(self):
        seed(self.conn, [500] * 25, trip_type="round_trip")
        seed(self.conn, [250] * 25, trip_type="one_way")
        s = baselines.route_stats(self.conn, "SJU", "MAD", "one_way",
                                  today=self.today)
        self.assertEqual(s["median"], 250)


class ClassifyTests(unittest.TestCase):
    def stats(self, n=40, median=500.0, p25=420.0, p10=380.0, ready=True):
        return {"ready": ready, "n_observations": n, "median": median,
                "p25": p25, "p10": p10}

    def test_not_ready_never_classifies(self):
        self.assertIsNone(baselines.classify(10, self.stats(ready=False)))

    def test_deal_hot_boundaries(self):
        s = self.stats()
        self.assertIsNone(baselines.classify(430, s))
        self.assertEqual(baselines.classify(410, s), "deal")
        self.assertEqual(baselines.classify(420, s), None)   # strict <
        self.assertEqual(baselines.classify(379, s), "hot")
        self.assertEqual(baselines.classify(380, s), "deal")  # strict <

    def test_mistake_tier(self):
        s = self.stats(n=30)
        self.assertEqual(baselines.classify(224, s), "mistake")  # < 45% of 500

    def test_mistake_requires_min_observations(self):
        # False-positive guard: even an absurdly low price must not fire the
        # mistake tier on a thin baseline — it downgrades to hot.
        s = self.stats(n=29)
        self.assertEqual(baselines.classify(224, s), "hot")
        self.assertEqual(baselines.classify(1, s), "hot")

    def test_mistake_boundary_is_strict(self):
        s = self.stats(n=40, median=1000.0, p25=900.0, p10=800.0)
        self.assertEqual(baselines.classify(450, s), "hot")      # not < 450
        self.assertEqual(baselines.classify(449.99, s), "mistake")


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)

    def test_first_alert_sends(self):
        self.assertTrue(baselines.should_send(self.conn, "MAD", "deal", 400,
                                              now=self.now))

    def test_repeat_within_cooldown_blocked(self):
        store.record_alert(self.conn, "MAD", "deal", 400,
                           (self.now - timedelta(hours=24)).isoformat())
        self.assertFalse(baselines.should_send(self.conn, "MAD", "deal", 400,
                                               now=self.now))
        self.assertFalse(baselines.should_send(self.conn, "MAD", "deal", 395,
                                               now=self.now))  # -1.25% only

    def test_eight_pct_drop_reopens(self):
        store.record_alert(self.conn, "MAD", "deal", 400,
                           (self.now - timedelta(hours=1)).isoformat())
        self.assertTrue(baselines.should_send(self.conn, "MAD", "deal", 368,
                                              now=self.now))   # exactly -8%
        self.assertFalse(baselines.should_send(self.conn, "MAD", "deal", 369,
                                               now=self.now))

    def test_cooldown_expiry_reopens(self):
        store.record_alert(self.conn, "MAD", "deal", 400,
                           (self.now - timedelta(hours=73)).isoformat())
        self.assertTrue(baselines.should_send(self.conn, "MAD", "deal", 400,
                                              now=self.now))

    def test_tiers_are_independent(self):
        store.record_alert(self.conn, "MAD", "deal", 400,
                           (self.now - timedelta(hours=1)).isoformat())
        self.assertTrue(baselines.should_send(self.conn, "MAD", "hot", 350,
                                              now=self.now))


if __name__ == "__main__":
    unittest.main()
