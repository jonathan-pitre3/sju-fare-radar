"""Weekly explore sweep — price-first discovery from SJU.

One-way searches across the config's explore destinations, three goldilocks
dates each (Tue/Wed departures). Every result is persisted
(source_job='explore', trip_type='one_way') and runs through the same tier
engine as the watch job: hot / mistake-fare finds alert immediately, and the
run always ends with one weekly digest of the cheapest finds.

Run weekly by .github/workflows/weekly.yml:
    python fare_radar/explore.py
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

import baselines
import store
from budget import Budget
from providers import get_provider

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())

DIGEST_TITLE = "✈️ Weekly radar from SJU"


def snap_to_tue_wed(target: date) -> date:
    """Next Tuesday on/after target; Wednesday if target already is one."""
    if target.weekday() == 2:      # Wednesday
        return target
    return target + timedelta(days=(1 - target.weekday()) % 7)


def sample_dates(weeks_out: list[int], today: date | None = None) -> list[date]:
    today = today or date.today()
    return [snap_to_tue_wed(today + timedelta(weeks=w)) for w in weeks_out]


def vs_typical(price: float, stats: dict) -> str:
    if not stats.get("ready"):
        return f"${price:.0f}"
    pct = round(100 * (price - stats["median"]) / stats["median"])
    return f"${price:.0f} (typically ~${stats['median']:.0f}, {pct:+d}%)"


def run() -> None:
    cfg = CONFIG.get("explore", {})
    if not cfg.get("enabled", True):
        print("explore: disabled in config")
        return
    s = CONFIG["settings"]
    bl_cfg = CONFIG.get("baselines", {})
    conn = store.connect()
    budget = Budget(conn, CONFIG.get("budget"))
    if budget.check_and_notify() == "exhausted":
        print(f"explore: budget exhausted ({budget.status_line()}), skipping")
        conn.commit()
        conn.close()
        return

    provider = get_provider(s.get("provider", "ignav"), counter=budget.counter,
                            excluded_providers=s.get("excluded_providers"))
    provider.job = "explore"
    origin = s["origin"]
    max_requests = cfg.get("max_requests_per_run", 160)
    spent_before = budget.spent

    finds = []      # cheapest offer per destination
    urgent = []     # hot / mistake alerts, dispatched immediately
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for haul, dests in cfg.get("destinations", {}).items():
        dates = sample_dates(cfg["weeks_out"][haul])
        for dest in dests:
            used = budget.spent - spent_before
            if used >= max_requests or budget.exhausted:
                print(f"explore: request cap reached ({used}), stopping sweep")
                break
            best = None
            for depart in dates:
                offers = provider.search_one_way(origin, dest, depart, s)
                if not offers:
                    continue
                for offer in offers:
                    store.record_observation(conn, {
                        **offer,
                        "observed_at": now, "origin": origin,
                        "destination": dest, "trip_type": "one_way",
                        "depart_date": depart.isoformat(), "return_date": None,
                        "carrier": "/".join(offer.get("carriers") or []) or None,
                        "source_job": "explore",
                    })
                top = {**offers[0], "depart": depart.isoformat()}
                if best is None or top["price"] < best["price"]:
                    best = top
            if best is None:
                continue
            stats = baselines.route_stats(conn, origin, dest, "one_way", bl_cfg)
            tier = baselines.classify(best["price"], stats, bl_cfg)
            key = f"{origin}→{dest} OW"
            best.update({"dest": dest, "tier": tier, "stats": stats, "key": key})
            finds.append(best)
            if tier in ("hot", "mistake") and baselines.should_send(
                    conn, key, tier, best["price"], bl_cfg):
                booking = None
                if best.get("ignav_id") and not budget.exhausted:
                    booking = provider.resolve_booking(best["ignav_id"])
                if booking and booking["excluded_only"]:
                    print(f"  {dest} {best['depart']}: [{tier}] only sold by an "
                          f"excluded seller, suppressed")
                    finds.remove(best)   # keep it out of the weekly digest too
                    continue
                if booking and booking["link"]:
                    best["link"] = booking["link"]
                store.record_alert(conn, key, tier, best["price"], now)
                urgent.append({
                    "at": now, "route": key, "label": dest, "origin": origin,
                    "price": best["price"], "tier": tier, "one_way": True,
                    "reason": vs_typical(best["price"], stats),
                    "confirmed": bool(best.get("confirmed")),
                    "self_transfer": bool(best.get("self_transfer")),
                    "depart": best["depart"], "return": None,
                    "stops": best.get("stops"),
                    "carriers": best["carriers"], "link": best["link"]})
        else:
            continue
        break   # inner cap-break also ends the outer loop

    if urgent:
        from alerts import dispatch
        dispatch(urgent)

    # Weekly digest: cheapest finds first; tier markers where earned.
    marks = {"mistake": "🚨 ", "hot": "🔥 ", "deal": "💰 "}
    lines = []
    for f in sorted(finds, key=lambda f: f["price"])[:cfg.get("digest_top_n", 12)]:
        lines.append(f"{marks.get(f['tier'], '')}{f['dest']} "
                     f"{vs_typical(f['price'], f['stats'])} OW — {f['depart']}"
                     + (" ⚠️ separate tickets" if f.get("self_transfer") else ""))
    used = budget.spent - spent_before
    if lines:
        from alerts import send_telegram_text
        send_telegram_text(f"{DIGEST_TITLE}\n\n" + "\n".join(lines) +
                           f"\n\n({len(finds)} destinations scanned · "
                           f"{used} requests · {budget.status_line()})")
    print(f"explore: {len(finds)} destinations, {len(urgent)} urgent alerts, "
          f"{used} requests used — {budget.status_line()}")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    run()
