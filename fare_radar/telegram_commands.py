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
    "• find anywhere cheap Nov 21-25\n"
    "• check Lisbon prices right now\n"
    "• keep searching until Nov 10\n"
    "• stop the Madrid watch\n\n"
    "Slash commands still work:\n"
    "/historial DEST — route stats for SJU → DEST\n"
    "/historial ORIGIN DEST — any tracked city pair\n"
    "/watches — your active standing watches\n"
    "/stop REF — stop a watch (id, destination, route, or 'all')\n"
    "/presupuesto — API request budget this month")

# When the conversational + watch layer is off (config watches.enabled: false),
# only these legacy stats commands answer; free text gets OFF_NOTICE.
HELP_OFF = (
    "✈️ Boti — SJU Fare Radar\n"
    "/historial DEST — route stats for SJU → DEST\n"
    "/historial ORIGIN DEST — any tracked city pair\n"
    "/presupuesto — API request budget this month")

OFF_NOTICE = (
    "🤖 The chat/watch features (natural-language requests and standing deal "
    "watches) are currently turned off.\n"
    "Working commands: /historial DEST, /presupuesto.\n"
    "(Re-enable by setting watches.enabled: true in config.yaml.)")


def feature_enabled() -> bool:
    """Master switch for the conversational bot + standing watches."""
    return bool(CONFIG.get("watches", {}).get("enabled", True))


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

SCOPE_LABEL = {"short": "short-haul (Caribbean/US/Central & northern S. America)",
               "long": "Europe + long-haul", "all": "anywhere tracked"}


def _target_label(w: dict) -> str:
    if w["destination"] == watches.ANY:
        return f"anywhere · {SCOPE_LABEL.get(w.get('scope', 'short'), w.get('scope'))}"
    return f"{w['origin']}→{w['destination']}"


def _fmt_watch(w: dict) -> str:
    cap = f" under ${w['max_price']:.0f}" if w.get("max_price") else ""
    window = (w["depart_from"] if w["depart_from"] == w["depart_to"]
              else f"{w['depart_from']}…{w['depart_to']}")
    tt = "OW" if w.get("trip_type") == "one_way" else "RT"
    trip = ""
    if w.get("return_length_days") and w.get("trip_type") != "one_way":
        trip = f" ({w['return_length_days']}-night)"
    return (f"[{w['id']}] {_target_label(w)} {tt}{trip} · "
            f"depart {window}{cap} · until {w['expires_at']} · {w['status']}")


def do_watch(intent: dict) -> str:
    s = CONFIG["settings"]
    wcfg = CONFIG.get("watches", {})
    dest = intent.get("destination")
    anywhere = bool(intent.get("anywhere")) or dest in (None, "", watches.ANY)
    if not anywhere and not dest:
        return ("Which destination should I watch? Or say 'anywhere' — e.g. "
                "'watch Madrid Nov 19-20 under $600 until I stop', or "
                "'find anywhere cheap Nov 21-25'.")
    depart_from = intent.get("depart_from")
    depart_to = intent.get("depart_to") or depart_from
    if not depart_from:
        return ("When are you free to travel? Give me a date or a range, e.g. "
                "'find anywhere cheap Nov 21-25'.")
    depart_from, depart_to = sorted([depart_from, depart_to])
    trip_len = (intent.get("return_length_days")
                or wcfg.get("default_return_length_days")
                or s["trip_length_days"])
    scope = (intent.get("scope")
             or wcfg.get("anywhere", {}).get("default_scope", "short"))
    try:
        w = watches.add_watch(
            intent.get("origin", s["origin"]),
            watches.ANY if anywhere else dest, depart_from, depart_to,
            trip_type=intent.get("trip_type", "round_trip"),
            return_length_days=trip_len, max_price=intent.get("max_price"),
            note=intent.get("question"), expires_at=intent.get("until"),
            scope=scope, max_active=wcfg.get("max_active", 20))
    except ValueError as exc:
        return f"Couldn't add that watch: {exc}"

    cap = (f"anything under ${w['max_price']:.0f}" if w.get("max_price")
           else "the cheapest finds and anything that clears the deal tiers")
    one_way = w.get("trip_type") == "one_way"
    if one_way:
        trip = f"one-way departing {w['depart_from']}"
    else:
        ret = (date.fromisoformat(w["depart_from"])
               + timedelta(days=w["return_length_days"])).isoformat()
        trip = f"round trip {w['depart_from']} → {ret}"
    stop_ref = "anywhere" if anywhere else w["destination"]
    return (
        "👀 " + _fmt_watch(w) + "\n"
        f"I'll flag {cap}, pricing this window on each daily radar run — "
        f"reading it as a {trip}.\n"
        f"🗓️ I'll keep searching until {w['expires_at']}, then stop automatically "
        "(your travel dates will have passed by then). Want a different cutoff? "
        f"Reply 'search until <date>'. Longer/shorter trip or a price cap? Just "
        f"tell me. Stop anytime with 'stop {stop_ref}'.")


def _norm_ref(ref: str) -> str:
    """Map friendly words to the store's matching tokens."""
    r = (ref or "").strip()
    if r.upper() in ("ANYWHERE", "ANY", "EVERYWHERE"):
        return watches.ANY
    return r


def do_update(intent: dict) -> str:
    until = intent.get("until")
    if not until:
        return ("Tell me the new stop date, e.g. 'keep searching until Nov 10'.")
    ref = _norm_ref(intent.get("watch_ref") or intent.get("destination") or "all")
    updated = watches.set_expiry(ref, until)
    if not updated:
        return f"No active watch matched '{ref}'. See them with /watches."
    return (f"🗓️ Updated — now searching until {until}:\n"
            + "\n".join(_fmt_watch(w) for w in updated))


def do_stop(intent: dict) -> str:
    ref = _norm_ref(intent.get("watch_ref") or intent.get("destination") or "")
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
    """Route one owner message to a reply. Legacy stats commands always work;
    the conversational + watch layer is gated by feature_enabled()."""
    parts = re.split(r"\s+", text.strip())
    cmd = parts[0].split("@")[0].lower()
    # Legacy stats commands — always available, no LLM, no writes.
    if cmd == "/historial":
        return historial(conn, parts[1:])
    if cmd == "/presupuesto":
        return presupuesto(conn)

    on = feature_enabled()
    if cmd in ("/help", "/start"):
        return HELP if on else HELP_OFF
    if not on:
        # Feature shelved: don't call the LLM or touch watches; just say so.
        return OFF_NOTICE
    if cmd in ("/watches", "/watchlist"):
        return do_list()
    if cmd == "/stop":
        return do_stop({"watch_ref": " ".join(parts[1:]).strip()})

    intent = nl.parse_intent(text, nl_context())
    if not intent:
        # No API key, parse failure, or an unrecognized slash command.
        return HELP
    action = intent["action"]
    if action == "watch":
        return do_watch(intent)
    if action == "update":
        return do_update(intent)
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
