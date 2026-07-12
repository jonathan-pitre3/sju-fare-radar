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


def _fmt(alert: dict) -> str:
    carriers = "/".join(alert["carriers"])
    grade = ("✅ CONFIRMED bookable at this price (checkout-grade, just now)"
             if alert.get("confirmed") else "Live search result — confirm at checkout")
    return (f"SJU → {alert['route']} ({alert['label']}) — ${alert['price']:.0f} RT\n"
            f"{alert['depart']} → {alert['return']} · {carriers}\n"
            f"{grade}\nWhy: {alert['reason']}\n"
            f"Open live fares: {alert['link']}")


def send_telegram(alerts: list[dict]) -> None:
    """Free forever — no Twilio, no per-message cost."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    body = "✈️ SJU Fare Radar\n\n" + "\n\n".join(_fmt(a) for a in alerts)
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
    msg.set_content("\n\n".join(_fmt(a) for a in alerts) +
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
    body = "✈️ SJU Fare Radar\n\n" + "\n\n".join(_fmt(a) for a in alerts)
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
