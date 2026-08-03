"""Unit tests for standing watches: the store, date sampling, and the
check_fares scan pass (tier engine + max_price cap + expiry), plus the offline
bits of the NL intent parser.

Run: .venv/bin/python -m unittest discover tests
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fare_radar"))

import baselines
import check_fares
import nl
import store
import watches
from budget import Budget


class TempWatchStore(unittest.TestCase):
    """Base: isolate each test to its own temp watches.json."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wpath = Path(self._tmp.name) / "watches.json"
        # load/save resolve WATCHES_PATH at call time, so this redirects the
        # module default (used by scan_watches' internal load/save too).
        self._orig = watches.WATCHES_PATH
        watches.WATCHES_PATH = self.wpath

    def tearDown(self):
        watches.WATCHES_PATH = self._orig
        self._tmp.cleanup()


class StoreTests(TempWatchStore):
    def test_add_and_list(self):
        w = watches.add_watch("SJU", "mad", "2035-11-19", "2035-11-20",
                              max_price=600)
        self.assertEqual(w["destination"], "MAD")   # uppercased
        self.assertEqual(w["status"], "active")
        self.assertEqual(w["expires_at"], "2035-11-20")   # defaults to depart_to
        self.assertEqual(len(watches.list_watches("active")), 1)

    def test_max_active_enforced(self):
        for i in range(2):
            watches.add_watch("SJU", "MAD", "2035-11-19", "2035-11-20")
        with self.assertRaises(ValueError):
            watches.add_watch("SJU", "FCO", "2035-11-19", "2035-11-20",
                              max_active=2)

    def test_stop_by_id_dest_route_and_all(self):
        a = watches.add_watch("SJU", "MAD", "2035-11-19", "2035-11-20")
        watches.add_watch("SJU", "FCO", "2035-11-19", "2035-11-20")
        watches.add_watch("SJU", "LIS", "2035-11-19", "2035-11-20")
        self.assertEqual(len(watches.stop_watch(a["id"])), 1)          # by id
        self.assertEqual(len(watches.stop_watch("fco")), 1)            # by dest
        self.assertEqual(len(watches.stop_watch("SJU→LIS")), 1)   # by route key
        self.assertEqual(watches.list_watches("active"), [])
        self.assertEqual(watches.stop_watch("MAD"), [])                # already stopped

    def test_stop_all(self):
        watches.add_watch("SJU", "MAD", "2035-11-19", "2035-11-20")
        watches.add_watch("SJU", "FCO", "2035-11-19", "2035-11-20")
        self.assertEqual(len(watches.stop_watch("all")), 2)
        self.assertEqual(watches.list_watches("active"), [])

    def test_expire_due(self):
        watches.add_watch("SJU", "MAD", "2020-01-01", "2020-01-02")   # past
        watches.add_watch("SJU", "FCO", "2035-11-19", "2035-11-20")   # future
        expired = watches.expire_due(today=date(2026, 8, 3))
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["destination"], "MAD")
        self.assertEqual({w["destination"] for w in watches.list_watches("active")},
                         {"FCO"})


class SampleDatesTests(unittest.TestCase):
    today = date(2026, 8, 3)

    def w(self, lo, hi):
        return {"depart_from": lo, "depart_to": hi}

    def test_short_window_every_day(self):
        got = watches.sample_dates(self.w("2026-11-19", "2026-11-20"), 4, self.today)
        self.assertEqual(got, [date(2026, 11, 19), date(2026, 11, 20)])

    def test_single_day(self):
        got = watches.sample_dates(self.w("2026-11-19", "2026-11-19"), 4, self.today)
        self.assertEqual(got, [date(2026, 11, 19)])

    def test_long_window_even_spread_with_endpoints(self):
        got = watches.sample_dates(self.w("2026-11-01", "2026-11-30"), 4, self.today)
        self.assertEqual(len(got), 4)
        self.assertEqual(got[0], date(2026, 11, 1))
        self.assertEqual(got[-1], date(2026, 11, 30))
        self.assertEqual(got, sorted(got))

    def test_past_window_returns_nothing(self):
        self.assertEqual(
            watches.sample_dates(self.w("2020-01-01", "2020-01-05"), 4, self.today),
            [])

    def test_clamped_to_future(self):
        got = watches.sample_dates(self.w("2026-08-01", "2026-08-05"), 10, self.today)
        self.assertTrue(all(d > self.today for d in got))


class FakeProvider:
    """Minimal stand-in for IgnavProvider — returns one fixed offer, no billing."""

    def __init__(self, price):
        self.job = "watch"
        self._price = price

    def _offer(self, origin, dest, depart_iso, ret_iso):
        return [{
            "price": self._price, "currency": "USD", "carriers": ["AA"],
            "marketing_carrier_code": "AA", "stops": 0, "duration_minutes": 600,
            "cabin_class": "economy", "self_transfer": False,
            "link": "https://example.test/fare", "confirmed": True,
            "ignav_id": None,
        }]

    def search(self, origin, dest, depart, ret, settings, market=None):
        return self._offer(origin, dest, depart.isoformat(), ret.isoformat())

    def search_one_way(self, origin, dest, depart, settings, market=None):
        return self._offer(origin, dest, depart.isoformat(), None)


class ScanWatchesTests(TempWatchStore):
    def setUp(self):
        super().setUp()
        self.conn = store.connect(":memory:")
        self.budget = Budget(self.conn, {"monthly_request_cap": 4000})
        self.settings = check_fares.CONFIG["settings"]
        self.now = store.utcnow()
        self.today = date(2026, 8, 3)

    def scan(self, price):
        pending = []
        check_fares.scan_watches(self.conn, FakeProvider(price), self.budget,
                                 self.settings, pending, self.now, today=self.today)
        return pending

    def test_max_price_cap_fires_without_baseline(self):
        watches.add_watch("SJU", "MAD", "2026-11-19", "2026-11-20", max_price=600)
        pending = self.scan(500)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["tier"], "legacy")
        self.assertIn("cap", pending[0]["reason"])
        self.assertIn("watch", pending[0]["reason"])
        # observation persisted + last_scanned stamped
        self.assertTrue(watches.list_watches("active")[0]["last_scanned"])
        n = self.conn.execute("SELECT COUNT(*) c FROM fare_observations "
                              "WHERE source_job='watch_adhoc'").fetchone()["c"]
        self.assertGreater(n, 0)

    def test_over_cap_no_alert(self):
        watches.add_watch("SJU", "MAD", "2026-11-19", "2026-11-20", max_price=400)
        self.assertEqual(self.scan(500), [])

    def test_tier_deal_fires_on_ready_baseline(self):
        # Seed a ready round-trip baseline for SJU->FCO: median ~500, p25 ~470.
        for i in range(30):
            at = (datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
                  + timedelta(days=i)).isoformat(timespec="seconds")
            store.record_observation(self.conn, {
                "observed_at": at, "origin": "SJU", "destination": "FCO",
                "trip_type": "round_trip", "depart_date": "2026-12-01",
                "return_date": "2026-12-08", "price": 500.0 + (i % 5),
                "currency": "USD", "source_job": "watch"})
        watches.add_watch("SJU", "FCO", "2026-12-01", "2026-12-02")  # no cap
        pending = self.scan(400)   # well under p25
        self.assertEqual(len(pending), 1)
        self.assertIn(pending[0]["tier"], ("deal", "hot"))

    def test_cooldown_blocks_second_scan(self):
        watches.add_watch("SJU", "MAD", "2026-11-19", "2026-11-20", max_price=600)
        self.assertEqual(len(self.scan(500)), 1)
        self.assertEqual(self.scan(500), [])   # same price, within cooldown

    def test_expired_window_marked_and_skipped(self):
        watches.add_watch("SJU", "MAD", "2020-01-01", "2020-01-02", max_price=600)
        self.assertEqual(self.scan(500), [])   # window is in the past
        self.assertEqual(watches.list_watches("active"), [])
        self.assertEqual(len(watches.list_watches("expired")), 1)

    def test_budget_exhausted_skips_pass(self):
        store.add_requests(self.conn, "watch", 4000)   # at cap
        watches.add_watch("SJU", "MAD", "2026-11-19", "2026-11-20", max_price=600)
        self.assertEqual(self.scan(500), [])
        # not scanned, so not stamped
        self.assertIsNone(watches.list_watches("active")[0]["last_scanned"])


class NormalizeTests(unittest.TestCase):
    def test_defaults_and_uppercasing(self):
        out = nl._normalize({"action": "ask", "destination": "mad"},
                            {"origin": "SJU"})
        self.assertEqual(out["origin"], "SJU")
        self.assertEqual(out["destination"], "MAD")
        self.assertEqual(out["trip_type"], "round_trip")

    def test_bad_action_becomes_help(self):
        out = nl._normalize({"action": "frobnicate"}, {"origin": "SJU"})
        self.assertEqual(out["action"], "help")

    def test_parse_intent_without_key_returns_none(self):
        import os
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            self.assertIsNone(nl.parse_intent("watch madrid", {"today": "2026-08-03"}))
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
