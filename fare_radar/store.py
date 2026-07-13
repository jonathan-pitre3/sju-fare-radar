"""SQLite persistence — every fare observation, alert state, request budget.

The database lives at data/fares.db and is committed back by the workflows
after each run (same mechanism as docs/data/history.json). history.json stays
the dashboard's data file; the DB is the system of record for baselines,
cooldowns, and the monthly request counter.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "fares.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fare_observations (
  id INTEGER PRIMARY KEY,
  observed_at TEXT NOT NULL,          -- ISO 8601 UTC
  origin TEXT NOT NULL,
  destination TEXT NOT NULL,
  trip_type TEXT NOT NULL,            -- 'one_way' | 'round_trip'
  depart_date TEXT NOT NULL,
  return_date TEXT,
  price REAL NOT NULL,
  currency TEXT NOT NULL,
  carrier TEXT,
  marketing_carrier_code TEXT,
  stops INTEGER,
  duration_minutes INTEGER,
  cabin_class TEXT,
  self_transfer INTEGER DEFAULT 0,
  ignav_id TEXT,
  source_job TEXT NOT NULL            -- 'watch' | 'explore' | 'flex' | 'positioning' | 'market_check'
);
CREATE INDEX IF NOT EXISTS idx_route ON fare_observations(origin, destination, trip_type);
CREATE INDEX IF NOT EXISTS idx_observed ON fare_observations(observed_at);

CREATE TABLE IF NOT EXISTS alerts_sent (
  id INTEGER PRIMARY KEY,
  route_key TEXT NOT NULL,            -- history key, e.g. 'MAD' or 'LAX→NRT'
  tier TEXT NOT NULL,                 -- 'deal' | 'hot' | 'mistake' | 'legacy'
  price REAL NOT NULL,
  sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_route ON alerts_sent(route_key, tier, sent_at);

CREATE TABLE IF NOT EXISTS request_log (
  id INTEGER PRIMARY KEY,
  month TEXT NOT NULL,                -- 'YYYY-MM' (UTC)
  job TEXT NOT NULL,
  n INTEGER NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_month ON request_log(month);

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

OBSERVATION_FIELDS = (
    "observed_at", "origin", "destination", "trip_type", "depart_date",
    "return_date", "price", "currency", "carrier", "marketing_carrier_code",
    "stops", "duration_minutes", "cabin_class", "self_transfer", "ignav_id",
    "source_job",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    if isinstance(path, Path):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record_observation(conn: sqlite3.Connection, obs: dict) -> None:
    row = {f: obs.get(f) for f in OBSERVATION_FIELDS}
    row["self_transfer"] = int(bool(row.get("self_transfer")))
    conn.execute(
        f"INSERT INTO fare_observations ({', '.join(OBSERVATION_FIELDS)}) "
        f"VALUES ({', '.join(':' + f for f in OBSERVATION_FIELDS)})", row)


def record_alert(conn: sqlite3.Connection, route_key: str, tier: str,
                 price: float, at: str | None = None) -> None:
    conn.execute("INSERT INTO alerts_sent (route_key, tier, price, sent_at) "
                 "VALUES (?, ?, ?, ?)", (route_key, tier, price, at or utcnow()))


def last_alert(conn: sqlite3.Connection, route_key: str, tier: str):
    return conn.execute(
        "SELECT price, sent_at FROM alerts_sent WHERE route_key = ? AND tier = ? "
        "ORDER BY sent_at DESC LIMIT 1", (route_key, tier)).fetchone()


def add_requests(conn: sqlite3.Connection, job: str, n: int = 1) -> None:
    now = utcnow()
    conn.execute("INSERT INTO request_log (month, job, n, at) VALUES (?, ?, ?, ?)",
                 (now[:7], job, n, now))


def month_requests(conn: sqlite3.Connection, month: str | None = None) -> int:
    month = month or utcnow()[:7]
    row = conn.execute("SELECT COALESCE(SUM(n), 0) AS total FROM request_log "
                       "WHERE month = ?", (month,)).fetchone()
    return row["total"]


def month_requests_by_job(conn: sqlite3.Connection,
                          month: str | None = None) -> dict[str, int]:
    month = month or utcnow()[:7]
    rows = conn.execute("SELECT job, SUM(n) AS total FROM request_log "
                        "WHERE month = ? GROUP BY job", (month,)).fetchall()
    return {r["job"]: r["total"] for r in rows}


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
