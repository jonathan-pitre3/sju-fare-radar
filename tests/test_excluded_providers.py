"""Unit tests for the excluded-seller policy in IgnavProvider.

Ignav search itineraries carry no seller attribution; the selling OTA only
appears in the booking-links response. So exclusion is enforced at
resolve_booking(), which is the single billable call made for alert-worthy
fares. These tests stub that call and assert the three outcomes:

  * a blocked seller is never linked (an allowed seller is used instead),
  * a fare sold ONLY by blocked sellers reports excluded_only=True,
  * a failed/empty lookup is treated as "unknown", never as "excluded".

Run: .venv/bin/python -m unittest discover tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fare_radar"))

import providers


def option(name, ptype, price, url):
    return {"provider_name": name, "provider_type": ptype,
            "price": {"amount": price, "currency": "USD", "status": "verified"},
            "url": url}


class FakeIgnav(providers.IgnavProvider):
    """IgnavProvider with the network POST replaced by a canned payload."""

    def __init__(self, payload, excluded_providers=None):
        super().__init__(counter=None, excluded_providers=excluded_providers)
        self._payload = payload

    def _post(self, path, payload, timeout=60):
        return self._payload


class ResolveBookingTests(unittest.TestCase):
    def _provider(self, options, excluded=("Holiday Breakz",)):
        payload = {"booking_options": [{"legs": ["outbound", "inbound"],
                                        "links": options}]}
        return FakeIgnav(payload, excluded_providers=list(excluded))

    def test_blocked_seller_is_replaced_by_allowed(self):
        p = self._provider([
            option("Holiday Breakz", "third_party", 200, "https://holidaybreakz/x"),
            option("Expedia", "third_party", 250, "https://expedia/x"),
        ])
        result = p.resolve_booking("ig1")
        self.assertFalse(result["excluded_only"])
        self.assertEqual(result["link"], "https://expedia/x")

    def test_airline_direct_preferred_over_allowed_ota(self):
        p = self._provider([
            option("Holiday Breakz", "third_party", 180, "https://holidaybreakz/x"),
            option("Expedia", "third_party", 210, "https://expedia/x"),
            option("American Airlines", "airline", 230, "https://aa.com/x"),
        ])
        result = p.resolve_booking("ig1")
        self.assertEqual(result["link"], "https://aa.com/x")

    def test_cheapest_allowed_ota_when_no_airline(self):
        p = self._provider([
            option("Holiday Breakz", "third_party", 100, "https://holidaybreakz/x"),
            option("Kiwi", "third_party", 260, "https://kiwi/x"),
            option("Expedia", "third_party", 240, "https://expedia/x"),
        ])
        result = p.resolve_booking("ig1")
        self.assertEqual(result["link"], "https://expedia/x")

    def test_excluded_only_when_every_seller_blocked(self):
        p = self._provider([
            option("Holiday Breakz", "third_party", 100, "https://holidaybreakz/x"),
            option("holiday breakz", "third_party", 105, "https://holidaybreakz/y"),
        ])
        result = p.resolve_booking("ig1")
        self.assertTrue(result["excluded_only"])
        self.assertIsNone(result["link"])

    def test_case_and_whitespace_insensitive_match(self):
        p = self._provider(
            [option("  holiday BREAKZ ", "third_party", 100, "https://hb/x")])
        result = p.resolve_booking("ig1")
        self.assertTrue(result["excluded_only"])

    def test_failed_lookup_is_unknown_not_excluded(self):
        # _post returning None models a network/budget failure — the fare must
        # be kept (caller falls back to the Google Flights link), never dropped.
        p = FakeIgnav(None, excluded_providers=["Holiday Breakz"])
        self.assertIsNone(p.resolve_booking("ig1"))

    def test_empty_booking_options_is_unknown_not_excluded(self):
        p = FakeIgnav({"booking_options": []},
                      excluded_providers=["Holiday Breakz"])
        result = p.resolve_booking("ig1")
        self.assertFalse(result["excluded_only"])
        self.assertIsNone(result["link"])

    def test_blocked_by_host_even_when_seller_name_differs(self):
        # The real Telegram deeplink lands on holidaybreakz.com but Ignav may
        # label the seller as a metasearch (CoreMeta/Wego). Host must catch it.
        real = ("https://www.holidaybreakz.com/deeplink/result/CoreMeta/"
                "YINS3I0O-6559961/1?wego_click_id=86ac9418fadc41859601b590c4c1e3cf")
        p = self._provider([
            option("CoreMeta", "third_party", 190, real),
            option("Expedia", "third_party", 240, "https://expedia/x"),
        ])
        result = p.resolve_booking("ig1")
        self.assertFalse(result["excluded_only"])
        self.assertEqual(result["link"], "https://expedia/x")

    def test_excluded_only_by_host_when_sole_seller(self):
        real = ("https://www.holidaybreakz.com/deeplink/result/CoreMeta/"
                "YINS3I0O-6559961/1?wego_click_id=86ac9418fadc41859601b590c4c1e3cf")
        p = self._provider([option("Wego", "third_party", 190, real)])
        result = p.resolve_booking("ig1")
        self.assertTrue(result["excluded_only"])
        self.assertIsNone(result["link"])

    def test_domain_style_blocklist_entry_matches(self):
        # A blocklist written as a domain works the same as the brand name.
        real = "https://www.holidaybreakz.com/deeplink/result/X/1"
        p = self._provider([option("Wego", "third_party", 190, real)],
                           excluded=("holidaybreakz.com",))
        self.assertTrue(p.resolve_booking("ig1")["excluded_only"])

    def test_no_blocklist_keeps_cheapest_behavior(self):
        p = self._provider([
            option("Holiday Breakz", "third_party", 100, "https://hb/x"),
            option("Expedia", "third_party", 250, "https://expedia/x"),
        ], excluded=())
        result = p.resolve_booking("ig1")
        self.assertFalse(result["excluded_only"])
        self.assertEqual(result["link"], "https://hb/x")


if __name__ == "__main__":
    unittest.main()
