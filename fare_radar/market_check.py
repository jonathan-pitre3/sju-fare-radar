"""Weekly market / point-of-sale check.

Re-queries a rotating subset of watched routes twice — once with Ignav's
`market` set to the destination country, once with the default US market,
both priced in USD — and flags fares that are >= beat_pct cheaper when sold
from the destination's point of sale. Rotation state lives in the kv table,
budget in request_log; the whole run stays within max_requests_per_run
(default 20: 8 routes x 2 searches, headroom for retries that never bill).

Run weekly by .github/workflows/weekly.yml after the explore sweep.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

import store
from budget import Budget
from providers import get_provider

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())


def rotation(all_routes: list[str], per_run: int, conn) -> list[str]:
    idx = int(store.kv_get(conn, "market_rotation_idx") or 0) % max(len(all_routes), 1)
    picked = [all_routes[(idx + i) % len(all_routes)]
              for i in range(min(per_run, len(all_routes)))]
    store.kv_set(conn, "market_rotation_idx", str((idx + per_run) % len(all_routes)))
    return picked


def run() -> None:
    cfg = CONFIG.get("market_check", {})
    if not cfg.get("enabled", True):
        print("market_check: disabled in config")
        return
    s = CONFIG["settings"]
    conn = store.connect()
    budget = Budget(conn, CONFIG.get("budget"))
    if budget.check_and_notify() == "exhausted":
        print(f"market_check: budget exhausted ({budget.status_line()}), skipping")
        conn.commit()
        conn.close()
        return

    markets = cfg.get("markets", {})
    watched = [r["code"] for r in CONFIG["routes"] if r["code"] in markets]
    if not watched:
        print("market_check: no watched routes have a market mapping")
        return
    picked = rotation(sorted(watched), cfg.get("routes_per_run", 8), conn)

    provider = get_provider(s.get("provider", "ignav"), counter=budget.counter)
    provider.job = "market_check"
    origin = s["origin"]
    depart = date.today() + timedelta(days=28)
    ret = depart + timedelta(days=s["trip_length_days"])
    beat = cfg.get("beat_pct", 15) / 100
    max_requests = cfg.get("max_requests_per_run", 20)
    spent_before = budget.spent
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    flagged = []
    for dest in picked:
        if budget.spent - spent_before >= max_requests or budget.exhausted:
            print("market_check: request cap reached, stopping")
            break
        us = provider.search(origin, dest, depart, ret, s, market="US")
        local = provider.search(origin, dest, depart, ret, s, market=markets[dest])
        for offers in (us, local):
            if offers:
                for offer in offers[:1]:
                    store.record_observation(conn, {
                        **offer, "observed_at": now, "origin": origin,
                        "destination": dest, "trip_type": "round_trip",
                        "depart_date": depart.isoformat(),
                        "return_date": ret.isoformat(),
                        "carrier": "/".join(offer.get("carriers") or []) or None,
                        "source_job": "market_check"})
        if not (us and local):
            continue
        us_best, local_best = us[0], local[0]
        if us_best["currency"] != local_best["currency"]:
            print(f"  {dest}: currency mismatch "
                  f"({us_best['currency']} vs {local_best['currency']}), skipped")
            continue
        if local_best["price"] <= us_best["price"] * (1 - beat):
            pct = round(100 * (us_best["price"] - local_best["price"])
                        / us_best["price"])
            flagged.append(f"{dest}: ${local_best['price']:.0f} sold from "
                           f"{markets[dest]} vs ${us_best['price']:.0f} from US "
                           f"(-{pct}%)")
            print(f"  {dest}: POS discrepancy -{pct}%")
        else:
            print(f"  {dest}: US ${us_best['price']:.0f} / "
                  f"{markets[dest]} ${local_best['price']:.0f} — no edge")

    if flagged:
        from alerts import send_telegram_text
        send_telegram_text(
            "🌍 Point-of-sale check — booking from the destination market is "
            "cheaper this week:\n\n" + "\n".join(flagged) +
            "\n\nBook via the airline's local site/currency; watch card FX fees.")
    used = budget.spent - spent_before
    print(f"market_check: {len(picked)} routes, {len(flagged)} flagged, "
          f"{used} requests — {budget.status_line()}")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    run()
