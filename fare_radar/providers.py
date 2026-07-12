"""Fare providers — swappable via config.yaml `provider:` key.

Lesson learned 2026-07-11: Amadeus killed its self-service portal with five
months' notice (keys die 2026-07-17). Never marry one API. Each provider
implements one function:

    search(origin, dest, depart: date, ret: date, settings) -> dict | None

returning {"price": float, "carriers": [str], "link": str, "confirmed": bool}
where "link" is a URL that shows live, bookable prices on click.

Response parsing verified against https://ignav.com/docs (round-trip,
booking-links, one-way, FAQ) on 2026-07-11: itineraries[].price is an object
{amount, currency, status}; carriers live on leg segments; booking links come
back under booking_options[].links[].url. Booking-link lookups bill as their
own request, so they are only fetched for alert-worthy fares (see
check_fares.py), not on every search.
"""

from __future__ import annotations

import os
import sys
from datetime import date

import requests


def google_flights_link(origin: str, dest: str, depart: str, ret: str) -> str:
    q = f"Flights from {origin} to {dest} on {depart} through {ret}"
    return "https://www.google.com/travel/flights?q=" + requests.utils.quote(q)


class IgnavProvider:
    """Default. Consumer-accurate fares + airline-direct booking links.

    Free tier: 1,000 requests/month; $2 per 1,000 after. Instant signup.
    Secrets required: IGNAV_API_KEY
    """

    BASE = "https://ignav.com/api"

    def _headers(self):
        return {"X-Api-Key": os.environ["IGNAV_API_KEY"],
                "Content-Type": "application/json"}

    @staticmethod
    def _carriers(itin) -> list[str]:
        codes = set()
        for leg in ("outbound", "inbound"):
            for seg in (itin.get(leg) or {}).get("segments", []):
                if seg.get("marketing_carrier_code"):
                    codes.add(seg["marketing_carrier_code"])
        return sorted(codes)

    def search(self, origin, dest, depart: date, ret: date, settings) -> dict | None:
        resp = requests.post(
            f"{self.BASE}/fares/round-trip",
            headers=self._headers(),
            json={"origin": origin, "destination": dest,
                  "departure_date": depart.isoformat(),
                  "return_date": ret.isoformat(),
                  "adults": settings["adults"]},
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"  ! ignav {origin}-{dest}: HTTP {resp.status_code}", file=sys.stderr)
            return None
        itins = resp.json().get("itineraries") or []
        priced = [i for i in itins if (i.get("price") or {}).get("amount") is not None]
        if not priced:
            return None
        cheapest = min(priced, key=lambda i: float(i["price"]["amount"]))
        price = cheapest["price"]

        return {"price": round(float(price["amount"]), 2),
                "carriers": self._carriers(cheapest),
                "link": google_flights_link(origin, dest,
                                            depart.isoformat(), ret.isoformat()),
                "confirmed": price.get("status") == "verified",
                "ignav_id": cheapest.get("ignav_id")}

    def booking_link(self, ignav_id: str) -> str | None:
        """Airline-direct booking URL. Bills as its own request — call sparingly."""
        try:
            resp = requests.post(f"{self.BASE}/fares/booking-links",
                                 headers=self._headers(),
                                 json={"ignav_id": ignav_id}, timeout=30)
            if resp.status_code != 200:
                return None
            links = [l for opt in resp.json().get("booking_options", [])
                     for l in opt.get("links", []) if l.get("url")]
            if not links:
                return None
            airline = [l for l in links if l.get("provider_type") == "airline"]
            return (airline or links)[0]["url"]
        except Exception:
            return None  # caller keeps the Google Flights link


class AmadeusProvider:
    """LEGACY — self-service keys die 2026-07-17. Kept for reference/Enterprise."""

    HOSTS = {"test": "https://test.api.amadeus.com",
             "production": "https://api.amadeus.com"}

    def __init__(self):
        self._token = None

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

    def search(self, origin, dest, depart: date, ret: date, settings) -> dict | None:
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
            return None
        offers = resp.json().get("data", [])
        if not offers:
            return None
        cheapest = min(offers, key=lambda o: float(o["price"]["grandTotal"]))
        carriers = sorted({s["carrierCode"] for it in cheapest["itineraries"]
                           for s in it["segments"]})
        return {"price": round(float(cheapest["price"]["grandTotal"]), 2),
                "carriers": carriers,
                "link": google_flights_link(origin, dest,
                                            depart.isoformat(), ret.isoformat()),
                "confirmed": False}


PROVIDERS = {"ignav": IgnavProvider, "amadeus": AmadeusProvider}


def get_provider(name: str):
    return PROVIDERS[name]()
