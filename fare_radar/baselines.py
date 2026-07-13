"""Per-route price baselines and tier classification.

Baselines group by (origin, destination, trip_type) — never by depart_date;
the baseline describes the route, not one travel date. Stats are computed
over a rolling observation window (default 120 days) at poll time.

A route's percentile tiers activate only once it has enough history
(default: >= 20 observations spanning >= 21 days). Below that the caller
falls back to the legacy static-threshold logic (cold-start mode).

Tiers (evaluated cheapest-first):
  mistake  price < mistake_median_ratio * median, needs n >= mistake_min_obs
  hot      price < p10   ("fire-sale" — alert with urgent formatting)
  deal     price < p25   (part of the normal digest)
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

DEFAULTS = {
    "window_days": 120,
    "min_observations": 20,
    "min_span_days": 21,
    "deal_percentile": 25,
    "hot_percentile": 10,
    "mistake_median_ratio": 0.45,
    "mistake_min_obs": 30,
}

TIER_ORDER = ("mistake", "hot", "deal")


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation percentile (inclusive), q in [0, 100]."""
    if not sorted_values:
        raise ValueError("percentile of empty list")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (q / 100) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def route_stats(conn: sqlite3.Connection, origin: str, destination: str,
                trip_type: str, cfg: dict | None = None,
                today: date | None = None) -> dict:
    """Rolling-window stats for one route. `ready` gates percentile alerts."""
    cfg = {**DEFAULTS, **(cfg or {})}
    today = today or datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=cfg["window_days"])).isoformat()
    rows = conn.execute(
        "SELECT price, observed_at FROM fare_observations "
        "WHERE origin = ? AND destination = ? AND trip_type = ? "
        "AND observed_at >= ? ORDER BY price",
        (origin, destination, trip_type, cutoff)).fetchall()
    prices = [r["price"] for r in rows]
    stats = {"origin": origin, "destination": destination, "trip_type": trip_type,
             "n_observations": len(prices), "median": None, "p25": None,
             "p10": None, "first_observed": None, "span_days": 0, "ready": False}
    if not prices:
        return stats
    observed = sorted(r["observed_at"] for r in rows)
    first, last = observed[0], observed[-1]
    span = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).days
    stats.update({
        "median": percentile(prices, 50),
        "p25": percentile(prices, cfg["deal_percentile"]),
        "p10": percentile(prices, cfg["hot_percentile"]),
        "first_observed": first,
        "span_days": span,
        "ready": (len(prices) >= cfg["min_observations"]
                  and span >= cfg["min_span_days"]),
    })
    return stats


def classify(price: float, stats: dict, cfg: dict | None = None) -> str | None:
    """Tier for a price against a route baseline; None if no tier (or not ready)."""
    cfg = {**DEFAULTS, **(cfg or {})}
    if not stats.get("ready"):
        return None
    if (price < stats["median"] * cfg["mistake_median_ratio"]
            and stats["n_observations"] >= cfg["mistake_min_obs"]):
        return "mistake"
    if price < stats["p10"]:
        return "hot"
    if price < stats["p25"]:
        return "deal"
    return None


def should_send(conn: sqlite3.Connection, route_key: str, tier: str,
                price: float, cfg: dict | None = None,
                now: datetime | None = None) -> bool:
    """Cooldown: re-alert same (route, tier) only after a further >= drop_ratio
    price drop or once cooldown_hours have passed."""
    cfg = cfg or {}
    cooldown = timedelta(hours=cfg.get("cooldown_hours", 72))
    drop_ratio = cfg.get("realert_drop_pct", 8) / 100
    now = now or datetime.now(timezone.utc)
    import store
    last = store.last_alert(conn, route_key, tier)
    if last is None:
        return True
    if now - datetime.fromisoformat(last["sent_at"]) >= cooldown:
        return True
    return price <= last["price"] * (1 - drop_ratio)
