"""Telegram command responder — polled at cron time, no server needed.

Boti stays broadcast-only during radar runs; this script answers commands
sent to the bot since the last poll:

    /historial ORIGIN DEST   route stats (median, p25, p10, n, best seen)
    /historial DEST          origin defaults to settings.origin (SJU)
    /presupuesto             monthly request-budget status

Replies land within the commands workflow cadence (every 2h), not instantly —
the repo has no webhook host by design. Processed updates are acknowledged
server-side by calling getUpdates with offset=last_id+1, so nothing needs to
be committed back. Only messages from TELEGRAM_CHAT_ID are answered.

Run by .github/workflows/commands.yml:
    python fare_radar/telegram_commands.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import requests
import yaml

import baselines
import store
from budget import Budget

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())

HELP = ("Commands:\n"
        "/historial DEST — route stats for SJU → DEST\n"
        "/historial ORIGIN DEST — any tracked city pair\n"
        "/presupuesto — API request budget this month")


def api(method: str, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    resp = requests.get(f"https://api.telegram.org/bot{token}/{method}",
                        params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("result", [])


def historial(conn, args: list[str]) -> str:
    s = CONFIG["settings"]
    if len(args) == 1:
        origin, dest = s["origin"], args[0].upper()
    elif len(args) == 2:
        origin, dest = args[0].upper(), args[1].upper()
    else:
        return HELP
    bl_cfg = CONFIG.get("baselines", {})
    lines = []
    for trip_type, tag in (("round_trip", "round trip"), ("one_way", "one way")):
        st = baselines.route_stats(conn, origin, dest, trip_type, bl_cfg)
        if not st["n_observations"]:
            continue
        best = conn.execute(
            "SELECT price, observed_at, depart_date FROM fare_observations "
            "WHERE origin = ? AND destination = ? AND trip_type = ? "
            "ORDER BY price LIMIT 1", (origin, dest, trip_type)).fetchone()
        lines.append(f"{origin} → {dest} ({tag})")
        lines.append(f"observations (last {bl_cfg.get('window_days', 120)}d): "
                     f"{st['n_observations']}")
        if st["median"] is not None:
            lines.append(f"typical ${st['median']:.0f} · p25 ${st['p25']:.0f} "
                         f"· p10 ${st['p10']:.0f}")
        lines.append(f"best seen: ${best['price']:.0f} "
                     f"(dep {best['depart_date']}, "
                     f"scanned {best['observed_at'][:10]})")
        lines.append("percentile alerts: " +
                     ("active ✅" if st["ready"] else
                      f"warming up ({st['n_observations']} obs / "
                      f"{st['span_days']}d span)"))
        lines.append("")
    return "\n".join(lines).strip() or \
        f"No observations for {origin} → {dest} yet."


def presupuesto(conn) -> str:
    budget = Budget(conn, CONFIG.get("budget"))
    by_job = store.month_requests_by_job(conn)
    lines = [f"📟 {budget.status_line()}"]
    for job, n in sorted(by_job.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {job}: {n}")
    lines.append(f"headroom: {budget.headroom()} requests "
                 f"(≈ ${budget.headroom() * 2 / 1000:.2f} unspent at Ignav rates)")
    lines.append("figures as of the last radar/explore run")
    return "\n".join(lines)


def run() -> None:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("commands: TELEGRAM_BOT_TOKEN not set, nothing to do")
        return
    my_chat = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    updates = api("getUpdates", timeout=0)
    if not updates:
        print("commands: no pending updates")
        return
    conn = store.connect()
    answered = 0
    for u in updates:
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id != my_chat or not text.startswith("/"):
            continue
        parts = re.split(r"\s+", text)
        cmd = parts[0].split("@")[0].lower()
        if cmd == "/historial":
            reply = historial(conn, parts[1:])
        elif cmd == "/presupuesto":
            reply = presupuesto(conn)
        else:
            reply = HELP
        api("sendMessage", chat_id=chat_id, text=reply[:4000])
        answered += 1
    # Server-side ack: Telegram forgets everything below this offset.
    api("getUpdates", offset=updates[-1]["update_id"] + 1, limit=1, timeout=0)
    conn.close()
    print(f"commands: {answered} answered of {len(updates)} updates")


if __name__ == "__main__":
    run()
