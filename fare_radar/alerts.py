"""Alert dispatch: email (SMTP) + WhatsApp (Twilio).

All credentials come from environment variables / GitHub Secrets.
Channels are optional — each one silently skips if its secrets are absent,
so you can start with email only and add WhatsApp later.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import requests


# Tier severity drives both the header and the sort order in a digest.
TIERS = {
    "mistake": {"header": "🚨 POSSIBLE MISTAKE FARE", "rank": 0},
    "hot":     {"header": "🔥 Hot deal", "rank": 1},
    "deal":    {"header": "💰 Deal", "rank": 2},
    "legacy":  {"header": None, "rank": 3},
}

DOT_REMINDER = ("Book it now, decide later — DOT rules give you 24 hours "
                "to cancel for free.")
SELF_TRANSFER_WARNING = ("⚠️ Separate tickets / self-transfer: no protection if "
                         "you miss a connection — leave a 4h+ buffer.")


def _stops(alert: dict) -> str | None:
    n = alert.get("stops")
    if n is None:
        return None
    return "nonstop" if n == 0 else f"{n} stop{'s' if n > 1 else ''}"


def _fmt(alert: dict) -> str:
    carriers = "/".join(alert["carriers"])
    grade = ("✅ CONFIRMED bookable at this price (checkout-grade, just now)"
             if alert.get("confirmed") else "Live search result — confirm at checkout")
    route = alert["route"]
    trip = "OW" if alert.get("one_way") else "RT"
    if alert.get("breakdown") or "→" in route:   # build name or origin-keyed leg
        head = route
        lines = [f"{head} — ${alert['price']:.0f} {trip}"]
    else:
        head = f"{alert.get('origin', 'SJU')} → {route}"
        lines = [f"{head} ({alert['label']}) — ${alert['price']:.0f} {trip}"]
    tier = TIERS.get(alert.get("tier") or "legacy", TIERS["legacy"])
    if tier["header"]:
        lines.insert(0, tier["header"])
    if alert.get("breakdown"):
        lines.append(f"Build: {alert['breakdown']} (book as separate tickets — DOT 24h rule)")
    when = alert["depart"] + (f" → {alert['return']}" if alert.get("return") else "")
    detail = f"{when} · {carriers}"
    if _stops(alert):
        detail += f" · {_stops(alert)}"
    lines += [detail, grade, f"Why: {alert['reason']}"]
    if alert.get("flex_note"):
        lines.append(f"📅 {alert['flex_note']}")
    if alert.get("positioning_note"):
        lines.append(f"🧩 {alert['positioning_note']}")
    if alert.get("self_transfer") and not alert.get("breakdown"):
        lines.append(SELF_TRANSFER_WARNING)
    if alert.get("tier") == "mistake":
        lines.append(DOT_REMINDER)
    for leg, url in (alert.get("leg_links") or {"": alert["link"]}).items():
        lines.append(f"Open live fares{f' {leg}' if leg else ''}: {url}")
    return "\n".join(lines)


def _sorted(alerts: list[dict]) -> list[dict]:
    return sorted(alerts, key=lambda a: (TIERS.get(a.get("tier") or "legacy",
                                                   TIERS["legacy"])["rank"],
                                         a["price"]))


def send_telegram_text(text: str) -> None:
    """Plain-text Telegram message (budget warnings, weekly digest)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:4000]},
        timeout=30,
    )
    print(f"Telegram dispatch: HTTP {resp.status_code}")


def send_telegram(alerts: list[dict], title: str = "✈️ SJU Fare Radar") -> None:
    """Free forever — no Twilio, no per-message cost."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    body = f"{title}\n\n" + "\n\n".join(_fmt(a) for a in _sorted(alerts))
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": body[:4000]},
        timeout=30,
    )
    print(f"Telegram dispatch: HTTP {resp.status_code}")


def send_email(alerts: list[dict]) -> None:
    host = os.environ.get("SMTP_HOST")
    if not host:
        return
    msg = EmailMessage()
    n = len(alerts)
    cheapest = min(a["price"] for a in alerts)
    msg["Subject"] = f"✈️ SJU Fare Radar: {n} alert{'s' if n > 1 else ''} (from ${cheapest:.0f})"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["ALERT_EMAIL_TO"]
    msg.set_content("\n\n".join(_fmt(a) for a in _sorted(alerts)) +
                    "\n\nPrices are live-queried at scan time; links open live "
                    "bookable fares. Verify final price at checkout.")
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465"))) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        smtp.send_message(msg)
    print(f"Email sent to {msg['To']}")


def send_whatsapp(alerts: list[dict]) -> None:
    sid = os.environ.get("TWILIO_SID")
    if not sid:
        return
    body = "✈️ SJU Fare Radar\n\n" + "\n\n".join(_fmt(a) for a in _sorted(alerts))
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, os.environ["TWILIO_TOKEN"]),
        data={
            "From": os.environ["TWILIO_WHATSAPP_FROM"],   # e.g. whatsapp:+14155238886
            "To": os.environ["WHATSAPP_TO"],              # e.g. whatsapp:+1787XXXXXXX
            "Body": body[:1500],
        },
        timeout=30,
    )
    print(f"WhatsApp dispatch: HTTP {resp.status_code}")


def dispatch(alerts: list[dict]) -> None:
    for sender in (send_email, send_whatsapp, send_telegram):
        try:
            sender(alerts)
        except Exception as exc:  # one channel failing must not kill the other
            print(f"{sender.__name__} failed: {exc}")
