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

from providers import get_provider

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
HISTORY_PATH = ROOT / "docs" / "data" / "history.json"


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
    provider = get_provider(s.get("provider", "ignav"))
    history = load_history()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pending = []
    thresholds = {}

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
            offer = provider.search(origin, code, depart, depart + leg_trip, s)
            if offer:
                offer.update({"depart": depart.isoformat(),
                              "return": (depart + leg_trip).isoformat()})
                results.append(offer)
        if not results:
            continue
        best = min(results, key=lambda r: r["price"])
        ignav_id = best.pop("ignav_id", None)
        run_best[key] = dict(best)

        entry = history["routes"].setdefault(
            key, {"label": label, "floor": None, "points": [], "last_alert": None})
        entry["label"] = label
        entry["origin"] = origin
        entry["kind"] = kind
        entry["points"].append({"at": now, **best})
        entry["points"] = entry["points"][-500:]

        reason = should_alert(entry, best["price"], route["alert_below"])
        if entry["floor"] is None or best["price"] < entry["floor"]:
            entry["floor"] = best["price"]
        if reason:
            # Alert-worthy: spend one extra billable call on an airline-direct link.
            if ignav_id and hasattr(provider, "booking_link"):
                direct = provider.booking_link(ignav_id)
                if direct:
                    best["link"] = direct
            alert = {"at": now, "route": key, "label": label, "origin": origin,
                     "price": best["price"], "reason": reason,
                     "confirmed": bool(best.get("confirmed")),
                     "depart": best["depart"], "return": best["return"],
                     "carriers": best["carriers"], "link": best["link"]}
            entry["last_alert"] = {"at": now, "price": best["price"]}
            history["alerts"] = ([alert] + history.get("alerts", []))[:100]
            pending.append(alert)
            print(f"  ALERT: ${best['price']} — {reason}")
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
        entry.update({"legs": legs, "versus": build.get("versus")})
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

    if pending:
        from alerts import dispatch
        dispatch(pending)


if __name__ == "__main__":
    run()
