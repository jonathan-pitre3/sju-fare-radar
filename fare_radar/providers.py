"""Fare providers — swappable via config.yaml `provider:` key.

Lesson learned 2026-07-11: Amadeus killed its self-service portal with five
months' notice (keys die 2026-07-17). Never marry one API. Each provider
implements:

    search(origin, dest, depart: date, ret: date, settings, market=None)
        -> list[dict]   (up to top_n offers, cheapest first, [] if none)
    search_one_way(origin, dest, depart: date, settings, market=None)
        -> list[dict]

Each offer dict carries price/carriers/link/confirmed plus the observation
fields persisted to SQLite (currency, stops, duration_minutes, cabin_class,
self_transfer, ignav_id, marketing_carrier_code).

Response parsing verified against https://ignav.com/docs (round-trip,
booking-links, one-way, FAQ) on 2026-07-11: itineraries[].price is an object
{amount, currency, status}; carriers live on leg segments; booking links come
back under booking_options[].links[].url. Booking-link lookups bill as their
own request, so they are only fetched for alert-worthy fares (see
check_fares.py), not on every search.

Billing: Ignav bills only successful (HTTP 200) requests. HTTP 424 and
network errors are retried with exponential backoff (max 3 attempts) and are
never counted against the budget. Empty itinerary arrays ARE successful
responses — they bill and they count.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date

import requests

TOP_N = 3          # offers returned per search (cheapest first)
MAX_TRIES = 3      # total attempts per request (424 / network errors)


def google_flights_link(origin: str, dest: str, depart: str,
                        ret: str | None = None) -> str:
    q = f"Flights from {origin} to {dest} on {depart}"
    if ret:
        q += f" through {ret}"
    else:
        q += " one way"
    return "https://www.google.com/travel/flights?q=" + requests.utils.quote(q)


class IgnavProvider:
    """Default. Consumer-accurate fares + airline-direct booking links.

    Free tier: 1,000 requests one-time; $2 per 1,000 after. Instant signup.
    Secrets required: IGNAV_API_KEY
    """

    BASE = "https://ignav.com/api"

    def __init__(self, counter=None):
        # counter(job, n) is called once per billable (HTTP 200) request so
        # the SQLite budget ledger matches Ignav's invoice.
        self.counter = counter
        self.job = "watch"

    def _headers(self):
        return {"X-Api-Key": os.environ["IGNAV_API_KEY"],
                "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict, timeout: int = 60):
        """POST with retry on HTTP 424 / network errors; counts 200s."""
        for attempt in range(1, MAX_TRIES + 1):
            try:
                resp = requests.post(f"{self.BASE}{path}", headers=self._headers(),
                                     json=payload, timeout=timeout)
            except requests.RequestException as exc:
                if attempt == MAX_TRIES:
                    print(f"  ! ignav {path}: {exc} (gave up)", file=sys.stderr)
                    return None
                time.sleep(2 ** (attempt - 1))
                continue
            if resp.status_code == 424 and attempt < MAX_TRIES:
                time.sleep(2 ** (attempt - 1))
                continue
            if resp.status_code != 200:
                print(f"  ! ignav {path}: HTTP {resp.status_code}", file=sys.stderr)
                return None
            if self.counter:
                self.counter(self.job, 1)
            return resp.json()
        return None

    @staticmethod
    def _carriers(itin) -> list[str]:
        codes = set()
        for leg in ("outbound", "inbound"):
            for seg in (itin.get(leg) or {}).get("segments", []):
                if seg.get("marketing_carrier_code"):
                    codes.add(seg["marketing_carrier_code"])
        return sorted(codes)

    @staticmethod
    def _leg_stats(itin) -> tuple[int | None, int | None, str | None]:
        """(stops, duration_minutes, cabin_class) across present legs."""
        stops = duration = None
        cabin = None
        for leg_name in ("outbound", "inbound"):
            leg = itin.get(leg_name)
            if not leg:
                continue
            segs = leg.get("segments", [])
            if segs:
                stops = (stops or 0) + max(len(segs) - 1, 0)
                cabin = cabin or segs[0].get("cabin_class")
            if leg.get("duration_minutes") is not None:
                duration = (duration or 0) + int(leg["duration_minutes"])
        return stops, duration, cabin

    def _offers(self, data, origin, dest, depart_iso, ret_iso, settings) -> list[dict]:
        itins = (data or {}).get("itineraries") or []
        priced = [i for i in itins if (i.get("price") or {}).get("amount") is not None]
        priced.sort(key=lambda i: float(i["price"]["amount"]))
        offers = []
        for itin in priced[:TOP_N]:
            price = itin["price"]
            stops, duration, cabin = self._leg_stats(itin)
            segs = (itin.get("outbound") or {}).get("segments", [])
            offers.append({
                "price": round(float(price["amount"]), 2),
                "currency": price.get("currency") or settings.get("currency", "USD"),
                "carriers": self._carriers(itin),
                "marketing_carrier_code": segs[0].get("marketing_carrier_code") if segs else None,
                "stops": stops,
                "duration_minutes": duration,
                "cabin_class": cabin,
                "self_transfer": bool(itin.get("self_transfer")),
                "link": google_flights_link(origin, dest, depart_iso, ret_iso),
                "confirmed": price.get("status") == "verified",
                "ignav_id": itin.get("ignav_id"),
            })
        return offers

    def search(self, origin, dest, depart: date, ret: date, settings,
               market: str | None = None) -> list[dict]:
        payload = {"origin": origin, "destination": dest,
                   "departure_date": depart.isoformat(),
                   "return_date": ret.isoformat(),
                   "adults": settings["adults"],
                   "currency": settings.get("currency", "USD"),
                   "allow_self_transfer": bool(settings.get("allow_self_transfer", True))}
        if market:
            payload["market"] = market
        data = self._post("/fares/round-trip", payload)
        return self._offers(data, origin, dest,
                            depart.isoformat(), ret.isoformat(), settings)

    def search_one_way(self, origin, dest, depart: date, settings,
                       market: str | None = None) -> list[dict]:
        payload = {"origin": origin, "destination": dest,
                   "departure_date": depart.isoformat(),
                   "adults": settings["adults"],
                   "currency": settings.get("currency", "USD"),
                   "allow_self_transfer": bool(settings.get("allow_self_transfer", True))}
        if market:
            payload["market"] = market
        data = self._post("/fares/one-way", payload)
        return self._offers(data, origin, dest, depart.isoformat(), None, settings)

    def booking_link(self, ignav_id: str) -> str | None:
        """Airline-direct booking URL. Bills as its own request — call sparingly."""
        data = self._post("/fares/booking-links", {"ignav_id": ignav_id}, timeout=30)
        if not data:
            return None
        links = [l for opt in data.get("booking_options", [])
                 for l in opt.get("links", []) if l.get("url")]
        if not links:
            return None
        airline = [l for l in links if l.get("provider_type") == "airline"]
        return (airline or links)[0]["url"]


class AmadeusProvider:
    """LEGACY — self-service keys die 2026-07-17. Kept for reference/Enterprise."""

    HOSTS = {"test": "https://test.api.amadeus.com",
             "production": "https://api.amadeus.com"}

    def __init__(self, counter=None):
        self._token = None
        self.counter = counter
        self.job = "watch"

    def _get_token(self, host):
        if self._token:
            return self._token
        resp = requests.post(f"{host}/v1/security/oauth2/token", data={
            "grant_type": "client_credentials",
            "client_id": os.environ["AMADEUS_API_KEY"],
            "client_secret": os.environ["AMADEUS_API_SECRET"]}, timeout=30)
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def search(self, origin, dest, depart: date, ret: date, settings,
               market: str | None = None) -> list[dict]:
        host = self.HOSTS[settings.get("amadeus_env", "test")]
        token = self._get_token(host)
        resp = requests.get(f"{host}/v2/shopping/flight-offers",
                            headers={"Authorization": f"Bearer {token}"},
                            params={"originLocationCode": origin,
                                    "destinationLocationCode": dest,
                                    "departureDate": depart.isoformat(),
                                    "returnDate": ret.isoformat(),
                                    "adults": settings["adults"],
                                    "currencyCode": settings["currency"],
                                    "max": 5}, timeout=60)
        if resp.status_code != 200:
            return []
        offers = resp.json().get("data", [])
        if self.counter:
            self.counter(self.job, 1)
        out = []
        for o in sorted(offers, key=lambda o: float(o["price"]["grandTotal"]))[:TOP_N]:
            carriers = sorted({s["carrierCode"] for it in o["itineraries"]
                               for s in it["segments"]})
            out.append({"price": round(float(o["price"]["grandTotal"]), 2),
                        "currency": settings.get("currency", "USD"),
                        "carriers": carriers,
                        "marketing_carrier_code": carriers[0] if carriers else None,
                        "stops": None, "duration_minutes": None,
                        "cabin_class": None, "self_transfer": False,
                        "link": google_flights_link(origin, dest, depart.isoformat(),
                                                    ret.isoformat()),
                        "confirmed": False, "ignav_id": None})
        return out

    def search_one_way(self, origin, dest, depart: date, settings,
                       market: str | None = None) -> list[dict]:
        return []  # legacy adapter: round-trip only


PROVIDERS = {"ignav": IgnavProvider, "amadeus": AmadeusProvider}


def get_provider(name: str, counter=None):
    return PROVIDERS[name](counter=counter)
