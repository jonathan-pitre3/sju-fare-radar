"""SJU Fare Radar — live fare checker (provider-agnostic).

Scans every route in config.yaml via the configured fare provider
(default: Ignav; see providers.py), records history, and alerts when a
fare crosses its threshold or sets a new floor. Every result ships with
a link that opens live, bookable prices.
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
HISTORY_PATH = ROOT / "docs" / "data" / "history.json"

# history.json points stay lean (dashboard payload); the full observation
# including duration/ignav_id goes to SQLite.
POINT_FIELDS = ("price", "carriers", "link", "confirmed", "stops")


def tier_reason(price: float, stats: dict) -> str:
    pct = round(100 * (price - stats["median"]) / stats["median"])
    return f"${price:.0f} — typically ~${stats['median']:.0f} ({pct:+d}%)"


def persist_offers(conn, origin: str, dest: str, offers: list[dict],
                   depart: date, ret: date | None, source_job: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for offer in offers:
        store.record_observation(conn, {
            **offer,
            "observed_at": now,
            "origin": origin,
            "destination": dest,
            "trip_type": "round_trip" if ret else "one_way",
            "depart_date": depart.isoformat(),
            "return_date": ret.isoformat() if ret else None,
            "carrier": "/".join(offer.get("carriers") or []) or None,
            "source_job": source_job,
        })


def sample_departure_dates() -> list[date]:
    s = CONFIG["settings"]
    today = date.today()
    n = max(1, s["samples_per_run"])
    step = max(7, s["scan_horizon_days"] // n)
    return [today + timedelta(days=14 + i * step) for i in range(n)]


def load_history() -> dict:
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text())
    return {"routes": {}, "alerts": []}


def should_alert(entry: dict, price: float, threshold: float) -> str | None:
    floor = entry.get("floor")
    cooldown = timedelta(hours=CONFIG["settings"]["alert_cooldown_hours"])
    last = entry.get("last_alert")
    if last:
        last_dt = datetime.fromisoformat(last["at"])
        if (datetime.now(timezone.utc) - last_dt) < cooldown and price >= last["price"]:
            return None
    if price <= threshold:
        return f"below your ${threshold:.0f} threshold"
    if floor is not None and price < floor:
        return f"new all-time observed floor (was ${floor:.2f})"
    return None


def run() -> None:
    s = CONFIG["settings"]
    conn = store.connect()
    budget = Budget(conn, CONFIG.get("budget"))
    provider = get_provider(s.get("provider", "ignav"), counter=budget.counter)
    history = load_history()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pending = []
    thresholds = {}

    region_of = {code: reg for reg, codes in CONFIG.get("regions", {}).items()
                 for code in codes}

    run_best = {}
    tagged = ([(r, "destination") for r in CONFIG["routes"]] +
              [(r, "positioning" if r.get("origin", s["origin"]) == s["origin"]
                   else "connector") for r in CONFIG.get("split_legs", [])])
    for route, kind in tagged:
        code, label = route["code"], route["label"]
        origin = route.get("origin", s["origin"])
        key = route.get("key") or (code if origin == s["origin"] else f"{origin}→{code}")
        thresholds[key] = route["alert_below"]
        # Buffer support: connecting legs depart offset days after the base date
        # (overnight self-transfer), positioning legs span a longer round trip.
        offset = timedelta(days=route.get("depart_offset_days", 0))
        leg_trip = timedelta(days=route.get("trip_days", s["trip_length_days"]))
        print(f"Scanning {origin} -> {code} ({label})")
        results = []
        for base in sample_departure_dates():
            depart = base + offset
            offers = provider.search(origin, code, depart, depart + leg_trip, s)
            if offers:
                persist_offers(conn, origin, code, offers,
                               depart, depart + leg_trip, "watch")
                best_of_date = dict(offers[0])
                best_of_date.update({"depart": depart.isoformat(),
                                     "return": (depart + leg_trip).isoformat()})
                results.append(best_of_date)
        if not results:
            continue
        best = min(results, key=lambda r: r["price"])
        ignav_id = best.get("ignav_id")
        self_transfer = best.get("self_transfer", False)
        best = {f: best[f] for f in (*POINT_FIELDS, "depart", "return")}
        if self_transfer:
            best["self_transfer"] = True
        run_best[key] = dict(best)

        entry = history["routes"].setdefault(
            key, {"label": label, "floor": None, "points": [], "last_alert": None})
        entry["label"] = label
        entry["origin"] = origin
        entry["kind"] = kind
        entry["region"] = region_of.get(code, "other")
        entry["points"].append({"at": now, **best})
        entry["points"] = entry["points"][-500:]

        # Tiered alerts against the route baseline; static threshold + floor
        # logic remains the fallback until the route has enough history.
        bl_cfg = CONFIG.get("baselines", {})
        stats = baselines.route_stats(conn, origin, code, "round_trip", bl_cfg)
        tier = reason = None
        if stats["ready"]:
            tier = baselines.classify(best["price"], stats, bl_cfg)
            if tier:
                if baselines.should_send(conn, key, tier, best["price"], bl_cfg):
                    reason = tier_reason(best["price"], stats)
                else:
                    print(f"  {tier} ${best['price']} — in cooldown, skipped")
                    tier = None
        else:
            reason = should_alert(entry, best["price"], route["alert_below"])
            tier = "legacy" if reason else None
        if entry["floor"] is None or best["price"] < entry["floor"]:
            entry["floor"] = best["price"]
        if tier:
            # Alert-worthy: spend one extra billable call on an airline-direct link.
            if ignav_id and not budget.exhausted and hasattr(provider, "booking_link"):
                direct = provider.booking_link(ignav_id)
                if direct:
                    best["link"] = direct
            alert = {"at": now, "route": key, "label": label, "origin": origin,
                     "price": best["price"], "reason": reason, "tier": tier,
                     "typical": stats["median"] if stats["ready"] else None,
                     "self_transfer": bool(best.get("self_transfer")),
                     "confirmed": bool(best.get("confirmed")),
                     "depart": best["depart"], "return": best["return"],
                     "stops": best.get("stops"),
                     "carriers": best["carriers"], "link": best["link"]}
            entry["last_alert"] = {"at": now, "price": best["price"]}
            store.record_alert(conn, key, tier, best["price"], now)
            history["alerts"] = ([alert] + history.get("alerts", []))[:100]
            pending.append(alert)
            print(f"  ALERT [{tier}]: ${best['price']} — {reason}")
        else:
            print(f"  best ${best['price']} ({best['depart']})")

    # Split builds: combined legs vs the direct single ticket.
    build_thresholds = {}
    for build in CONFIG.get("builds", []):
        name, legs = build["name"], build["legs"]
        build_thresholds[name] = build["alert_below"]
        missing = [l for l in legs if l not in run_best]
        if missing:
            print(f"Build {name}: no data for {missing}, skipped")
            continue
        combined = round(sum(run_best[l]["price"] for l in legs), 2)
        direct = run_best.get(build.get("versus"), {}).get("price")

        entry = history.setdefault("builds", {}).setdefault(
            name, {"floor": None, "points": [], "last_alert": None})
        final_dest = (build.get("versus") or legs[-1]).split("→")[-1].rstrip("+")
        entry.update({"legs": legs, "versus": build.get("versus"),
                      "region": region_of.get(final_dest, "other")})
        entry["points"].append({"at": now, "price": combined, "direct": direct})
        entry["points"] = entry["points"][-500:]

        reason = should_alert(entry, combined, build["alert_below"])
        if entry["floor"] is None or combined < entry["floor"]:
            entry["floor"] = combined
        vs = f" vs ${direct:.0f} direct" if direct else ""
        suppressed = False
        if reason and direct is not None and combined >= direct:
            # A build that doesn't beat the single ticket isn't actionable.
            print(f"Build {name}: ${combined}{vs} — {reason}, "
                  f"suppressed (direct is cheaper)")
            reason, suppressed = None, True
        if reason:
            first = run_best[legs[0]]
            alert = {"at": now, "route": name, "label": name, "origin": s["origin"],
                     "tier": "legacy", "self_transfer": True,
                     "price": combined, "reason": reason + vs,
                     "confirmed": all(run_best[l].get("confirmed") for l in legs),
                     "depart": first["depart"], "return": first["return"],
                     "carriers": sorted({c for l in legs for c in run_best[l]["carriers"]}),
                     "link": first["link"],
                     "breakdown": " + ".join(f"{l} ${run_best[l]['price']:.0f}" for l in legs),
                     "leg_links": {l: run_best[l]["link"] for l in legs}}
            entry["last_alert"] = {"at": now, "price": combined}
            history["alerts"] = ([alert] + history.get("alerts", []))[:100]
            pending.append(alert)
            print(f"Build {name}: ALERT ${combined}{vs} — {reason}")
        elif not suppressed:
            print(f"Build {name}: ${combined}{vs}")

    # Drop history for routes/builds removed from config so the dashboard
    # doesn't show permanently stale rows.
    history["routes"] = {k: v for k, v in history["routes"].items() if k in thresholds}
    if "builds" in history:
        history["builds"] = {k: v for k, v in history["builds"].items()
                             if k in build_thresholds}

    history["updated_at"] = now
    history["thresholds"] = thresholds
    history["build_thresholds"] = build_thresholds
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=1))
    budget.check_and_notify()
    print(f"Budget: {budget.status_line()}")
    conn.commit()
    conn.close()

    if pending:
        from alerts import dispatch
        dispatch(pending)


if __name__ == "__main__":
    run()
