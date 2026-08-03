"""Standing, date-bounded fare watches — "be free Nov 19–20, keep hunting until I stop."

A watch is a user-created deal monitor for a specific travel window that rides the
daily radar (check_fares.py) until it expires or the user stops it. Unlike config
`routes` (which the rotating rake prices across the whole calendar), a watch pins an
explicit departure window, so check_fares spends a small, budget-capped batch of live
searches on it each run.

State lives in a plain-text JSON file (data/watches.json), NOT the SQLite DB:
human-readable, diff-friendly/mergeable, inspectable, and future dashboard-ready —
mirroring how docs/data/history.json is the dashboard payload. It is committed
alongside data/fares.db by the (now serialized) commands + radar workflows.

Created/stopped by the Telegram bot (telegram_commands.py); read, scanned, and
expired by check_fares.py.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHES_PATH = ROOT / "data" / "watches.json"

# A watch record's fields (documented for the store; add_watch fills them).
FIELDS = (
    "id", "created_at", "origin", "destination", "trip_type",
    "depart_from", "depart_to", "return_length_days", "max_price",
    "note", "status", "expires_at", "last_scanned",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path | str | None = None) -> dict:
    p = Path(path or WATCHES_PATH)
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    data.setdefault("watches", [])
    return data


def save(data: dict, path: Path | str | None = None) -> None:
    p = Path(path or WATCHES_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1))


def route_key(watch: dict) -> str:
    """`ORIGIN→DEST` — matches the history keying convention in check_fares."""
    return f"{watch['origin']}→{watch['destination']}"


def _new_id(existing: list[dict]) -> str:
    """Short, stable, sortable id: w_<yymmddHHMMSS>[-N] on collision. Plain
    Python (server-side) — no sandbox clock restrictions here."""
    base = "w_" + datetime.now(timezone.utc).strftime("%y%m%d%H%M%S")
    ids = {w.get("id") for w in existing}
    if base not in ids:
        return base
    i = 2
    while f"{base}-{i}" in ids:
        i += 1
    return f"{base}-{i}"


def add_watch(origin: str, destination: str, depart_from: str, depart_to: str,
              *, trip_type: str = "round_trip", return_length_days: int | None = None,
              max_price: float | None = None, note: str | None = None,
              expires_at: str | None = None, max_active: int = 20,
              path: Path | str | None = None) -> dict:
    """Create and persist a watch. `expires_at` defaults to depart_to (once the
    window has passed, the watch is done). Raises ValueError past `max_active`."""
    data = load(path)
    active = [w for w in data["watches"] if w.get("status") == "active"]
    if len(active) >= max_active:
        raise ValueError(f"already at the {max_active}-active-watch limit; "
                         f"stop one first")
    watch = {
        "id": _new_id(data["watches"]),
        "created_at": _utcnow(),
        "origin": origin.upper(),
        "destination": destination.upper(),
        "trip_type": "one_way" if trip_type == "one_way" else "round_trip",
        "depart_from": depart_from,
        "depart_to": depart_to,
        "return_length_days": return_length_days,
        "max_price": max_price,
        "note": note,
        "status": "active",
        "expires_at": expires_at or depart_to,
        "last_scanned": None,
    }
    data["watches"].append(watch)
    save(data, path)
    return watch


def list_watches(status: str | None = "active",
                 path: Path | str | None = None) -> list[dict]:
    watches = load(path)["watches"]
    if status is None:
        return list(watches)
    return [w for w in watches if w.get("status") == status]


def stop_watch(ref: str, path: Path | str | None = None) -> list[dict]:
    """Deactivate matching active watches. `ref` is 'all', a watch id, a bare
    destination ('MAD'), or a route key ('SJU→MAD'), case-insensitive. Returns
    the watches that were stopped."""
    data = load(path)
    ref_u = (ref or "").strip().upper()
    stopped = []
    for w in data["watches"]:
        if w.get("status") != "active":
            continue
        if (ref_u == "ALL"
                or w["id"].upper() == ref_u
                or w["destination"] == ref_u
                or route_key(w).upper() == ref_u):
            w["status"] = "stopped"
            w["stopped_at"] = _utcnow()
            stopped.append(w)
    if stopped:
        save(data, path)
    return stopped


def expire_due(today: date | None = None,
               path: Path | str | None = None) -> list[dict]:
    """Flip active watches to 'expired' once today is past their expires_at.
    Returns the watches that were expired (persists only if any changed)."""
    today = today or datetime.now(timezone.utc).date()
    data = load(path)
    expired = []
    for w in data["watches"]:
        if w.get("status") != "active":
            continue
        exp = w.get("expires_at")
        if exp and today > date.fromisoformat(exp):
            w["status"] = "expired"
            expired.append(w)
    if expired:
        save(data, path)
    return expired


def sample_dates(watch: dict, max_dates: int, today: date | None = None) -> list[date]:
    """Departure dates to price inside [depart_from, depart_to], future-only.
    Each day when the window is short; an even spread (endpoints included) when
    it is longer than max_dates."""
    today = today or datetime.now(timezone.utc).date()
    lo = date.fromisoformat(watch["depart_from"])
    hi = date.fromisoformat(watch["depart_to"])
    if hi < lo:
        lo, hi = hi, lo
    lo = max(lo, today + timedelta(days=1))
    if hi < lo:
        return []
    span_days = (hi - lo).days
    n_days = span_days + 1
    max_dates = max(1, int(max_dates))
    if n_days <= max_dates:
        return [lo + timedelta(days=i) for i in range(n_days)]
    if max_dates == 1:
        return [lo]
    # Even spread across the window, endpoints included.
    step = span_days / (max_dates - 1)
    picked = sorted({lo + timedelta(days=round(i * step))
                     for i in range(max_dates)})
    return picked
