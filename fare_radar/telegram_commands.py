"""Telegram command responder — polled at cron time, no server needed.

Boti stays broadcast-only during radar runs; this script answers messages sent
to the bot since the last poll. It understands both the legacy slash commands
and plain natural-language requests (parsed by nl.py via the Anthropic API):

    /historial ORIGIN DEST   route stats (median, p25, p10, n, best seen)
    /historial DEST          origin defaults to settings.origin (SJU)
    /presupuesto             monthly request-budget status
    /watches                 list your active standing watches
    /stop REF                stop a watch (id, destination, route, or 'all')

    ...or just type, e.g.:
    "how cheap does Rome usually get?"
    "watch Madrid Nov 19-20 under $600 until I say stop"
    "stop the Madrid watch"

Standing "watches" are date-bounded deal monitors (see watches.py). This script
creates/stops them; the daily radar (check_fares.py) scans and expires them.

Replies land within the commands workflow cadence (every 2h), not instantly —
the repo has no webhook host by design. Processed updates are acknowledged
server-side by calling getUpdates with offset=last_id+1, so nothing needs to be
committed back for the ack. Watch changes and any live-search observations ARE
persisted (data/watches.json / data/fares.db); the workflow commits them, which
is why commands.yml now shares the radar/weekly concurrency group.

Only messages from TELEGRAM_CHAT_ID are answered.

Run by .github/workflows/commands.yml:
    python fare_radar/telegram_commands.py
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

import baselines
import nl
import store
import watches
from budget import Budget

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())

HELP = (
    "✈️ Boti — SJU Fare Radar\n"
    "Just type what you want, for example:\n"
    "• how cheap does Rome usually get?\n"
    "• watch Madrid Nov 19-20 under $600 until I say stop\n"
    "• check Lisbon prices right now\n"
    "• stop the Madrid watch\n\n"
    "Slash commands still work:\n"
    "/historial DEST — route stats for SJU → DEST\n"
    "/historial ORIGIN DEST — any tracked city pair\n"
    "/watches — your active standing watches\n"
    "/stop REF — stop a watch (id, destination, route, or 'all')\n"
    "/presupuesto — API request budget this month")


def api(method: str, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    resp = requests.get(f"https://api.telegram.org/bot{token}/{method}",
                        params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("result", [])


# --- Answers from stored history (free) -----------------------------------

def route_answer(conn, origin: str, dest: str) -> str:
    """Route stats for one city pair from the stored baselines — the shared
    body behind /historial and natural-language 'ask'."""
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


def historial(conn, args: list[str]) -> str:
    s = CONFIG["settings"]
    if len(args) == 1:
        origin, dest = s["origin"], args[0].upper()
    elif len(args) == 2:
        origin, dest = args[0].upper(), args[1].upper()
    else:
        return HELP
    return route_answer(conn, origin, dest)


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


# --- Live price on request (bills one Ignav search) ------------------------

def live_price(conn, intent: dict) -> str:
    """Run a single live search for an 'ask ... right now' request and format
    the current best fare + booking link. Budget-gated like any scan."""
    s = CONFIG["settings"]
    budget = Budget(conn, CONFIG.get("budget"))
    if budget.exhausted:
        return ("(live price skipped — monthly request budget is exhausted; "
                "showing history only)")
    if not os.environ.get("IGNAV_API_KEY"):
        return "(live price unavailable — IGNAV_API_KEY not set)"
    from providers import get_provider
    provider = get_provider(s.get("provider", "ignav"), counter=budget.counter,
                            excluded_providers=s.get("excluded_providers"))
    provider.job = "ask"
    origin = intent.get("origin", s["origin"])
    dest = intent["destination"]
    one_way = intent.get("trip_type") == "one_way"
    today = datetime.now(timezone.utc).date()
    if intent.get("depart_from"):
        depart = date.fromisoformat(intent["depart_from"])
        if depart <= today:
            depart = today + timedelta(days=s.get("rake_min_days", 14))
    else:
        depart = today + timedelta(days=45)
    trip_len = intent.get("return_length_days") or s["trip_length_days"]
    ret = None if one_way else depart + timedelta(days=trip_len)

    if one_way:
        offers = provider.search_one_way(origin, dest, depart, s)
    else:
        offers = provider.search(origin, dest, depart, ret, s)
    if not offers:
        return f"live check {origin}→{dest} {depart}: no fares returned right now."
    best = offers[0]
    # Persist so the observation counts like any scan and enriches baselines.
    now = store.utcnow()
    store.record_observation(conn, {
        **best, "observed_at": now, "origin": origin, "destination": dest,
        "trip_type": "one_way" if one_way else "round_trip",
        "depart_date": depart.isoformat(),
        "return_date": ret.isoformat() if ret else None,
        "carrier": "/".join(best.get("carriers") or []) or None,
        "source_job": "ask"})
    link = best["link"]
    if best.get("ignav_id") and hasattr(provider, "resolve_booking"):
        resolved = provider.resolve_booking(best["ignav_id"])
        if resolved and resolved.get("link"):
            link = resolved["link"]
    when = depart.isoformat() + (f" → {ret.isoformat()}" if ret else "")
    carriers = "/".join(best.get("carriers") or []) or "?"
    grade = "✅ CONFIRMED" if best.get("confirmed") else "live search result"
    return (f"live now: ${best['price']:.0f} "
            f"{'OW' if one_way else 'RT'} · {when} · {carriers} ({grade})\n{link}")


# --- Standing watches ------------------------------------------------------

def _fmt_watch(w: dict) -> str:
    cap = f" under ${w['max_price']:.0f}" if w.get("max_price") else ""
    window = (w["depart_from"] if w["depart_from"] == w["depart_to"]
              else f"{w['depart_from']}…{w['depart_to']}")
    tt = "OW" if w.get("trip_type") == "one_way" else "RT"
    return (f"[{w['id']}] {w['origin']}→{w['destination']} {tt} · "
            f"depart {window}{cap} · {w['status']}")


def do_watch(intent: dict) -> str:
    s = CONFIG["settings"]
    wcfg = CONFIG.get("watches", {})
    dest = intent.get("destination")
    if not dest:
        return ("Which destination should I watch? Try: "
                "'watch Madrid Nov 19-20 under $600 until I say stop'.")
    depart_from = intent.get("depart_from")
    depart_to = intent.get("depart_to") or depart_from
    if not depart_from:
        return ("When are you free to travel? Give me a date or a range, e.g. "
                f"'watch {dest} Nov 19-20 until I say stop'.")
    depart_from, depart_to = sorted([depart_from, depart_to])
    trip_len = (intent.get("return_length_days")
                or wcfg.get("default_return_length_days")
                or s["trip_length_days"])
    try:
        w = watches.add_watch(
            intent.get("origin", s["origin"]), dest, depart_from, depart_to,
            trip_type=intent.get("trip_type", "round_trip"),
            return_length_days=trip_len, max_price=intent.get("max_price"),
            note=intent.get("question"),
            max_active=wcfg.get("max_active", 20))
    except ValueError as exc:
        return f"Couldn't add that watch: {exc}"
    cap = (f" I'll flag anything under ${w['max_price']:.0f}"
           if w.get("max_price") else " I'll flag anything that clears the deal tiers")
    return ("👀 Watching " + _fmt_watch(w) + ".\n"
            f"{cap}, checking your window on each daily radar run until "
            f"{w['expires_at']} or you say stop. Say 'stop " + dest +
            "' to cancel.")


def do_stop(intent: dict) -> str:
    ref = intent.get("watch_ref") or intent.get("destination") or ""
    if not ref:
        return ("Which watch should I stop? Give an id, a destination, or 'all'. "
                "See them with /watches.")
    stopped = watches.stop_watch(ref)
    if not stopped:
        return f"No active watch matched '{ref}'. See active ones with /watches."
    return "🛑 Stopped:\n" + "\n".join(_fmt_watch(w) for w in stopped)


def do_list() -> str:
    active = watches.list_watches("active")
    if not active:
        return "No active watches. Add one, e.g. 'watch Madrid Nov 19-20 until I stop'."
    return "👀 Active watches:\n" + "\n".join(_fmt_watch(w) for w in active)


# --- Dispatch --------------------------------------------------------------

def nl_context() -> dict:
    s = CONFIG["settings"]
    routes = ", ".join(f"{r['code']} — {r.get('label', r['code'])}"
                       for r in CONFIG.get("routes", []))
    return {
        "today": datetime.now(timezone.utc).date().isoformat(),
        "origin": s["origin"],
        "routes": routes,
        "model": CONFIG.get("watches", {}).get("llm_model"),
    }


def handle(conn, text: str) -> str:
    """Route one owner message to a reply. Slash commands first (free, no LLM),
    then natural language via nl.parse_intent."""
    parts = re.split(r"\s+", text.strip())
    cmd = parts[0].split("@")[0].lower()
    if cmd == "/historial":
        return historial(conn, parts[1:])
    if cmd == "/presupuesto":
        return presupuesto(conn)
    if cmd in ("/watches", "/watchlist"):
        return do_list()
    if cmd == "/stop":
        return do_stop({"watch_ref": " ".join(parts[1:]).strip()})
    if cmd in ("/help", "/start"):
        return HELP

    intent = nl.parse_intent(text, nl_context())
    if not intent:
        # No API key, parse failure, or an unrecognized slash command.
        return HELP
    action = intent["action"]
    if action == "watch":
        return do_watch(intent)
    if action == "stop":
        return do_stop(intent)
    if action == "list":
        return do_list()
    if action == "ask" and intent.get("destination"):
        answer = route_answer(conn, intent.get("origin"), intent["destination"])
        if intent.get("live"):
            answer += "\n\n" + live_price(conn, intent)
        return answer
    return HELP


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
        if chat_id != my_chat or not text:
            continue
        try:
            reply = handle(conn, text)
        except Exception as exc:  # never let one bad message wedge the poll
            print(f"commands: error handling {text!r}: {exc}")
            reply = "Sorry — something went wrong handling that. Try /help."
        api("sendMessage", chat_id=chat_id, text=reply[:4000])
        answered += 1
    conn.commit()
    # Server-side ack: Telegram forgets everything below this offset.
    api("getUpdates", offset=updates[-1]["update_id"] + 1, limit=1, timeout=0)
    conn.close()
    print(f"commands: {answered} answered of {len(updates)} updates")


if __name__ == "__main__":
    run()
