"""Monthly request-budget guardrail.

Ignav bills successful (HTTP 200) requests only; the provider layer counts
exactly those into request_log. This module reads that ledger:

  - at >= warn_at_pct of the cap, Boti gets a one-time warning (per month)
  - at 100%, non-watch jobs (explore / flex / positioning / market check)
    must skip themselves and Boti is told (once per month)

The daily watch job keeps running even over cap — stopping it would blind the
baselines — but its alert links stop spending extra booking-link calls.
"""

from __future__ import annotations

import sqlite3

import store

DEFAULTS = {"monthly_request_cap": 4000, "warn_at_pct": 80}


class Budget:
    def __init__(self, conn: sqlite3.Connection, cfg: dict | None = None):
        self.conn = conn
        cfg = {**DEFAULTS, **(cfg or {})}
        self.cap = cfg["monthly_request_cap"]
        self.warn_at = cfg["warn_at_pct"] / 100

    def counter(self, job: str, n: int = 1) -> None:
        store.add_requests(self.conn, job, n)

    @property
    def spent(self) -> int:
        return store.month_requests(self.conn)

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.cap

    def headroom(self) -> int:
        return max(self.cap - self.spent, 0)

    def status_line(self) -> str:
        spent = self.spent
        pct = 100 * spent / self.cap if self.cap else 0
        return f"{spent}/{self.cap} requests this month ({pct:.0f}%)"

    def _once_per_month(self, flag: str) -> bool:
        """True the first time `flag` fires in the current month."""
        month = store.utcnow()[:7]
        key = f"budget_{flag}"
        if store.kv_get(self.conn, key) == month:
            return False
        store.kv_set(self.conn, key, month)
        return True

    def check_and_notify(self) -> str:
        """Returns 'ok' | 'warn' | 'exhausted'; notifies Boti on transitions."""
        from alerts import send_telegram_text
        if self.exhausted:
            if self._once_per_month("exhausted"):
                send_telegram_text(
                    f"🛑 SJU Fare Radar: request budget exhausted — "
                    f"{self.status_line()}. Explore/flex/positioning/market "
                    f"jobs are paused until next month; daily watch continues.")
            return "exhausted"
        if self.spent >= self.cap * self.warn_at:
            if self._once_per_month("warn"):
                send_telegram_text(
                    f"⚠️ SJU Fare Radar: {self.status_line()} — approaching "
                    f"the monthly cap. Non-watch jobs pause at 100%.")
            return "warn"
        return "ok"
