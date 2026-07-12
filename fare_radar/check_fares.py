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


def should_alert(history: dict, code: str, price: float, threshold: float) -> str | None:
    route = history["routes"].get(code, {})
    floor = route.get("floor")
    cooldown = timedelta(hours=CONFIG["settings"]["alert_cooldown_hours"])
    last = route.get("last_alert")
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
    trip_len = timedelta(days=s["trip_length_days"])
    pending = []
    thresholds = {}

    for route in CONFIG["routes"] + CONFIG.get("split_legs", []):
        code, label = route["code"], route["label"]
        thresholds[code] = route["alert_below"]
        print(f"Scanning {s['origin']} -> {code} ({label})")
        results = []
        for depart in sample_departure_dates():
            offer = provider.search(s["origin"], code, depart, depart + trip_len, s)
            if offer:
                offer.update({"depart": depart.isoformat(),
                              "return": (depart + trip_len).isoformat()})
                results.append(offer)
        if not results:
            continue
        best = min(results, key=lambda r: r["price"])
        ignav_id = best.pop("ignav_id", None)

        entry = history["routes"].setdefault(
            code, {"label": label, "floor": None, "points": [], "last_alert": None})
        entry["label"] = label
        entry["points"].append({"at": now, **best})
        entry["points"] = entry["points"][-500:]

        reason = should_alert(history, code, best["price"], route["alert_below"])
        if entry["floor"] is None or best["price"] < entry["floor"]:
            entry["floor"] = best["price"]
        if reason:
            # Alert-worthy: spend one extra billable call on an airline-direct link.
            if ignav_id and hasattr(provider, "booking_link"):
                direct = provider.booking_link(ignav_id)
                if direct:
                    best["link"] = direct
            alert = {"at": now, "route": code, "label": label,
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

    history["updated_at"] = now
    history["thresholds"] = thresholds
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=1))

    if pending:
        from alerts import dispatch
        dispatch(pending)


if __name__ == "__main__":
    run()
