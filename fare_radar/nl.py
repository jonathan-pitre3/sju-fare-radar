"""Natural-language intent parsing for the Telegram bot.

Turns a free-text message ("watch madrid nov 19-20 under 600 until i say stop",
"how cheap does Rome usually get?", "stop the Madrid watch") into a structured
intent the command handler can act on. Uses the Anthropic Messages API with a
single output tool, so the model is forced to return validated JSON — no brittle
prose parsing. Reuses the `requests` dependency already in the repo; no new pip.

Design:
  - Only the repo owner's own messages ever reach here (telegram_commands filters
    on TELEGRAM_CHAT_ID first), so no untrusted text triggers an API call or spend.
  - parse_intent returns None when ANTHROPIC_API_KEY is unset or the call fails;
    the caller then falls back to legacy slash commands / a help reply. The bot
    keeps working as before without the key.
"""

from __future__ import annotations

import json
import os

import requests

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Single tool the model must call — its input IS the parsed intent.
INTENT_TOOL = {
    "name": "record_intent",
    "description": "Record the parsed intent of the user's flight-deal message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["ask", "watch", "stop", "list", "help"],
                "description": (
                    "ask = a question answerable from price history or a live "
                    "check; watch = create a standing deal monitor for a travel "
                    "window; stop = cancel a watch; list = show active watches; "
                    "help = unclear / greeting / anything else."),
            },
            "origin": {
                "type": "string",
                "description": "Origin IATA code (3 letters). Default SJU if unstated.",
            },
            "destination": {
                "type": "string",
                "description": ("Destination IATA code (3 letters). Resolve city "
                                "names to their main airport, e.g. Madrid->MAD, "
                                "Tokyo->NRT, Rome->FCO. Null if none named."),
            },
            "trip_type": {
                "type": "string",
                "enum": ["round_trip", "one_way"],
                "description": "Default round_trip unless the user says one-way.",
            },
            "depart_from": {
                "type": "string",
                "description": ("Earliest departure date, ISO YYYY-MM-DD, resolved "
                                "against today's date. Null if none given."),
            },
            "depart_to": {
                "type": "string",
                "description": ("Latest departure date, ISO YYYY-MM-DD. If the user "
                                "names a single date, set both depart_from and "
                                "depart_to to it. Null if none given."),
            },
            "return_length_days": {
                "type": "integer",
                "description": "Trip length in days if stated (round trips). Else null.",
            },
            "max_price": {
                "type": "number",
                "description": "Price ceiling in USD if the user gave one, else null.",
            },
            "live": {
                "type": "boolean",
                "description": ("True only if the user explicitly wants the current/"
                                "live price right now (e.g. 'check now', 'what is it "
                                "today'). For general 'how cheap is X usually', false."),
            },
            "watch_ref": {
                "type": "string",
                "description": ("For action=stop: which watch to cancel — 'all', a "
                                "watch id, or a destination/route like MAD. Null "
                                "otherwise."),
            },
            "question": {
                "type": "string",
                "description": "For action=ask: the user's question, lightly cleaned up.",
            },
        },
        "required": ["action"],
    },
}


def _system_prompt(ctx: dict) -> str:
    today = ctx.get("today", "")
    origin = ctx.get("origin", "SJU")
    routes = ctx.get("routes", "")
    return (
        "You parse messages sent to a personal flight-deal Telegram bot and call "
        "record_intent with the structured result. Always call the tool exactly "
        "once; never reply in prose.\n"
        f"Today's date is {today}. The traveler's home origin is {origin} — use it "
        "when no origin is stated. Resolve relative dates ('next month', 'the 19th "
        "to the 20th', 'Nov 19-20') against today. Interpret two-digit day ranges in "
        "the same month. Resolve city/country names to IATA codes. Prices are USD.\n"
        f"Routes this bot already tracks (code — label): {routes}\n"
        "If the user is clearly setting up an ongoing watch ('keep looking', 'until "
        "I tell you to stop', 'let me know if it drops'), use action=watch. If they "
        "ask what a route costs or how cheap it gets, use action=ask."
    )


def parse_intent(text: str, ctx: dict | None = None,
                 model: str | None = None, timeout: int = 30) -> dict | None:
    """Parse `text` into an intent dict, or None if unavailable (no key / error).

    ctx may carry: today (ISO date str), origin (default IATA), routes (str
    listing tracked codes/labels), model (override).
    """
    ctx = ctx or {}
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not text.strip():
        return None
    payload = {
        "model": model or ctx.get("model") or DEFAULT_MODEL,
        "max_tokens": 400,
        "temperature": 0,
        "system": _system_prompt(ctx),
        "tools": [INTENT_TOOL],
        "tool_choice": {"type": "tool", "name": "record_intent"},
        "messages": [{"role": "user", "content": text.strip()[:1000]}],
    }
    try:
        resp = requests.post(
            API_URL,
            headers={"x-api-key": api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload, timeout=timeout)
        if resp.status_code != 200:
            print(f"nl: anthropic HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as exc:
        print(f"nl: request failed: {exc}")
        return None

    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "record_intent":
            return _normalize(block.get("input") or {}, ctx)
    return None


def _normalize(intent: dict, ctx: dict) -> dict:
    """Tidy the model output: uppercase codes, default origin, sane action."""
    out = dict(intent)
    action = (out.get("action") or "help").lower()
    if action not in ("ask", "watch", "stop", "list", "help"):
        action = "help"
    out["action"] = action
    out["origin"] = (out.get("origin") or ctx.get("origin", "SJU")).upper()
    if out.get("destination"):
        out["destination"] = out["destination"].upper()
    if out.get("trip_type") not in ("round_trip", "one_way"):
        out["trip_type"] = "round_trip"
    return out
