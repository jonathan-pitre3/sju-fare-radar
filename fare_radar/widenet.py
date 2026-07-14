"""Stage-1 wide net: Aviasales cached calendars → Ignav live verification.

The Going model in miniature. The free Travelpayouts/Aviasales Data API
serves cached prices (from real user searches, ~48h fresh) for every day of
a month in one request — the calendar coverage Ignav can't give us. Those
cached prices are hints, not quotes: they never enter fare_observations or
the baselines. Instead, any cached day priced under the route's own p25
becomes a candidate, and only candidates get a billable live Ignav search.
Verified fares persist (source_job='widenet') and run through the normal
tier engine.

Needs TRAVELPAYOUTS_TOKEN (repo secret); skips silently without it.
Runs inside radar.yml after the daily watch scan:
    python fare_radar/widenet.py
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

import baselines
import store
from budget import Budget
from providers import get_provider

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())

TP_URL = "https://api.travelpayouts.com/v1/prices/calendar"


def parse_calendar(payload: dict) -> list[tuple[date, float]]:
    """(depart_date, price) pairs from a /v1/prices/calendar response."""
    if not payload or not payload.get("success"):
        return []
    out = []
    for day, row in (payload.get("data") or {}).items():
        try:
            out.append((date.fromisoformat(day), float(row["price"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def fetch_calendar(token: str, origin: str, dest: str, month: str,
                   length: int, currency: str = "usd") -> list[tuple[date, float]]:
    try:
        resp = requests.get(TP_URL, headers={"X-Access-Token": token},
                            params={"origin": origin, "destination": dest,
                                    "depart_date": month, "length": length,
                                    "calendar_type": "departure_date",
                                    "currency": currency}, timeout=30)
        if resp.status_code != 200:
            return []
        return parse_calendar(resp.json())
    except requests.RequestException:
        return []


def run() -> None:
    cfg = CONFIG.get("widenet", {})
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not cfg.get("enabled", True):
        print("widenet: disabled in config")
        return
    if not token:
        print("widenet: TRAVELPAYOUTS_TOKEN not set, skipping "
              "(add it as a repo secret to activate the wide net)")
        return
    s = CONFIG["settings"]
    bl_cfg = CONFIG.get("baselines", {})
    conn = store.connect()
    budget = Budget(conn, CONFIG.get("budget"))
    if budget.check_and_notify() == "exhausted":
        print(f"widenet: budget exhausted ({budget.status_line()}), skipping")
        conn.commit()
        conn.close()
        return

    provider = get_provider(s.get("provider", "ignav"), counter=budget.counter)
    provider.job = "widenet"
    origin = s["origin"]
    trip = timedelta(days=s["trip_length_days"])
    months_ahead = cfg.get("months_ahead", 4)
    max_verifications = cfg.get("max_verifications_per_run", 10)
    today = date.today()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Candidates: (gap ratio, dest, date, cached price) — verify biggest gaps first.
    candidates = []
    scanned = 0
    for route in CONFIG["routes"]:
        dest = route["code"]
        stats = baselines.route_stats(conn, origin, dest, "round_trip", bl_cfg)
        if not stats["ready"]:
            continue    # no trusted yardstick yet — skip, the watch job is building it
        for m in range(1, months_ahead + 1):
            month_start = (today.replace(day=1) + timedelta(days=32 * m)).replace(day=1)
            days = fetch_calendar(token, origin, dest,
                                  month_start.isoformat(), s["trip_length_days"])
            scanned += 1
            time.sleep(0.3)     # be polite to the free API
            for d, cached in days:
                if d <= today or cached >= stats["p25"]:
                    continue
                candidates.append((cached / stats["median"], dest, d, cached, stats))

    candidates.sort()
    verified_alerts = []
    verifications = 0
    seen_routes = set()
    for _, dest, d, cached, stats in candidates:
        if verifications >= max_verifications or budget.exhausted:
            break
        if dest in seen_routes:     # one verification per route per run
            continue
        seen_routes.add(dest)
        offers = provider.search(origin, dest, d, d + trip, s)
        verifications += 1
        if not offers:
            print(f"  {dest} {d}: cache said ${cached:.0f}, no live fare")
            continue
        live = offers[0]
        persist_offers_widenet(conn, origin, dest, offers, d, d + trip, now)
        tier = baselines.classify(live["price"], stats, bl_cfg)
        if tier and not baselines.should_send(conn, dest, tier, live["price"], bl_cfg):
            print(f"  {dest} {d}: live ${live['price']:.0f} is [{tier}] but "
                  f"in cooldown (already alerted), skipped")
            continue
        if tier:
            if live.get("ignav_id") and not budget.exhausted:
                direct = provider.booking_link(live["ignav_id"])
                if direct:
                    live["link"] = direct
            store.record_alert(conn, dest, tier, live["price"], now)
            verified_alerts.append({
                "at": now, "route": dest,
                "label": f"{dest} (wide-net find, verified live)",
                "origin": origin, "price": live["price"], "tier": tier,
                "reason": (f"${live['price']:.0f} — typically "
                           f"~${stats['median']:.0f} "
                           f"({round(100 * (live['price'] - stats['median']) / stats['median']):+d}%)"),
                "typical": stats["median"],
                "self_transfer": bool(live.get("self_transfer")),
                "confirmed": bool(live.get("confirmed")),
                "depart": d.isoformat(), "return": (d + trip).isoformat(),
                "stops": live.get("stops"),
                "carriers": live["carriers"], "link": live["link"]})
            print(f"  {dest} {d}: ALERT [{tier}] cache ${cached:.0f} → live ${live['price']:.0f}")
        else:
            print(f"  {dest} {d}: cache ${cached:.0f} → live ${live['price']:.0f}, "
                  f"not below tier thresholds")

    if verified_alerts:
        from alerts import dispatch
        dispatch(verified_alerts)
    print(f"widenet: {scanned} calendars scanned (free), {len(candidates)} "
          f"candidates, {verifications} verified (billable), "
          f"{len(verified_alerts)} alerts — {budget.status_line()}")
    conn.commit()
    conn.close()


def persist_offers_widenet(conn, origin, dest, offers, depart, ret, now) -> None:
    for offer in offers:
        store.record_observation(conn, {
            **offer, "observed_at": now, "origin": origin, "destination": dest,
            "trip_type": "round_trip", "depart_date": depart.isoformat(),
            "return_date": ret.isoformat(),
            "carrier": "/".join(offer.get("carriers") or []) or None,
            "source_job": "widenet"})


if __name__ == "__main__":
    run()
