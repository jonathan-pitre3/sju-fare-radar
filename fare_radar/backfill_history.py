"""One-time import of docs/data/history.json into data/fares.db.

Route points become fare_observations rows (source_job='watch'). Build points
are skipped: builds are derived sums of leg fares, not real itineraries, and
their legs are already imported individually. Safe to re-run — rows already
present are skipped.

Usage: python fare_radar/backfill_history.py
"""

from __future__ import annotations

import json
from pathlib import Path

import store

ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = ROOT / "docs" / "data" / "history.json"


def key_endpoints(key: str, entry: dict) -> tuple[str, str]:
    """History key → (origin, destination). Keys: 'MAD', 'MAD+', 'LAX→NRT'."""
    if "→" in key:
        origin, dest = key.split("→", 1)
        return origin, dest.rstrip("+")
    return entry.get("origin", "SJU"), key.rstrip("+")


def run() -> None:
    history = json.loads(HISTORY_PATH.read_text())
    conn = store.connect()
    inserted = skipped = 0
    for key, entry in history.get("routes", {}).items():
        origin, dest = key_endpoints(key, entry)
        for p in entry.get("points", []):
            exists = conn.execute(
                "SELECT 1 FROM fare_observations WHERE observed_at = ? AND "
                "origin = ? AND destination = ? AND depart_date = ? AND price = ?",
                (p["at"], origin, dest, p.get("depart", ""), p["price"])).fetchone()
            if exists:
                skipped += 1
                continue
            store.record_observation(conn, {
                "observed_at": p["at"],
                "origin": origin,
                "destination": dest,
                "trip_type": "round_trip",
                "depart_date": p.get("depart", ""),
                "return_date": p.get("return"),
                "price": p["price"],
                "currency": "USD",
                "carrier": "/".join(p.get("carriers") or []) or None,
                "source_job": "watch",
            })
            inserted += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) AS c FROM fare_observations").fetchone()["c"]
    conn.close()
    print(f"Backfill: {inserted} inserted, {skipped} already present, "
          f"{total} observations total")


if __name__ == "__main__":
    run()
