"""Tests for the Going-style upgrades: calendar rake, date windows,
wide-net calendar parsing."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fare_radar"))

import check_fares
import widenet


class RakeTests(unittest.TestCase):
    def test_bounds(self):
        for day in range(0, 400, 17):
            today = date(2026, 7, 14) + timedelta(days=day)
            for d in check_fares.rake_dates(today, 2, 14, 270):
                off = (d - today).days
                self.assertGreaterEqual(off, 14)
                self.assertLess(off, 270)

    def test_rotates_daily(self):
        a = check_fares.rake_dates(date(2026, 7, 14), 2, 14, 270)
        b = check_fares.rake_dates(date(2026, 7, 15), 2, 14, 270)
        self.assertNotEqual([(d - date(2026, 7, 14)).days for d in a],
                            [(d - date(2026, 7, 15)).days for d in b])

    def test_curve_coverage_over_cycle(self):
        """Within ~4 weeks the near stratum is sampled across its span."""
        offsets = set()
        for day in range(28):
            today = date(2026, 7, 14) + timedelta(days=day)
            near = check_fares.rake_dates(today, 2, 14, 270)[0]
            offsets.add((near - today).days)
        self.assertGreaterEqual(len(offsets), 20)          # mostly distinct
        self.assertGreater(max(offsets) - min(offsets), 90)  # spans the stratum

    def test_weekday_rotation(self):
        weekdays = {check_fares.rake_dates(date(2026, 7, 14) + timedelta(days=i),
                                           1, 14, 270)[0].weekday()
                    for i in range(14)}
        self.assertGreaterEqual(len(weekdays), 5)

    def test_deterministic(self):
        self.assertEqual(check_fares.rake_dates(date(2026, 7, 14), 2, 14, 270),
                         check_fares.rake_dates(date(2026, 7, 14), 2, 14, 270))


class WindowTests(unittest.TestCase):
    ANCHOR = date(2026, 9, 25)

    def test_no_neighbors_no_note(self):
        self.assertIsNone(check_fares.summarize_window(self.ANCHOR, 300, []))

    def test_expensive_neighbors_no_note(self):
        probes = [(self.ANCHOR + timedelta(days=7), 400)]
        self.assertIsNone(check_fares.summarize_window(self.ANCHOR, 300, probes, 15))

    def test_window_spans_qualifying_dates(self):
        probes = [(self.ANCHOR - timedelta(days=14), 330),   # within +15% of 300
                  (self.ANCHOR - timedelta(days=7), 500),    # out
                  (self.ANCHOR + timedelta(days=7), 310)]    # in
        note = check_fares.summarize_window(self.ANCHOR, 300, probes, 15)
        self.assertIn("Sep 11", note)
        self.assertIn("Oct 02", note)
        self.assertIn("3 of 4", note)

    def test_tolerance_boundary(self):
        probes = [(self.ANCHOR + timedelta(days=7), 345.0)]  # exactly +15%
        self.assertIsNotNone(check_fares.summarize_window(self.ANCHOR, 300, probes, 15))


class ParseCalendarTests(unittest.TestCase):
    def test_parses_valid_days(self):
        payload = {"success": True, "data": {
            "2026-08-28": {"price": 209, "transfers": 0, "airline": "F9"},
            "2026-08-29": {"price": 311.5, "transfers": 1},
        }}
        out = widenet.parse_calendar(payload)
        self.assertEqual(out, [(date(2026, 8, 28), 209.0),
                               (date(2026, 8, 29), 311.5)])

    def test_failure_and_garbage(self):
        self.assertEqual(widenet.parse_calendar({"success": False, "data": {}}), [])
        self.assertEqual(widenet.parse_calendar({}), [])
        payload = {"success": True, "data": {
            "not-a-date": {"price": 100},
            "2026-08-28": {"transfers": 0},          # no price
            "2026-08-29": {"price": "cheap"},        # bad price
        }}
        self.assertEqual(widenet.parse_calendar(payload), [])


if __name__ == "__main__":
    unittest.main()
