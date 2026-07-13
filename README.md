# ✈️ SJU Fare Radar

Your personal Google Flights-style price watcher for flights departing **San Juan (SJU)**.
Live fares via the Ignav API, scanned automatically once a day by GitHub Actions,
displayed on a personal dashboard (GitHub Pages), with email / WhatsApp / Telegram alerts
when a route drops below your threshold or sets a new price floor. Every fare ships with
a link that opens live, bookable prices — alerts get an airline-direct booking link when
Ignav can produce one, otherwise a live Google Flights query. Never a cached price.

Companion to `sju-cheap-flight-search-strategy.md`, which seeds the thresholds and
holds the routing heuristics (positioning hubs, split-ticket rules, booking guide).

> Provider history: originally built on Amadeus Self-Service, which shut down
> (keys die 2026-07-17). The provider layer is now swappable (`providers.py`);
> Ignav is the default and the Amadeus adapter is kept as legacy reference.

## Architecture

```
GitHub Actions (cron, 1x/day, free)
   └─ fare_radar/check_fares.py ── Ignav fares API (live prices, verified status)
        ├─ docs/data/history.json   (price history → dashboard)
        ├─ alerts.py → email (SMTP) + WhatsApp (Twilio) + Telegram
        └─ links: airline-direct (alerts) / Google Flights live query
GitHub Pages serves docs/ → your dashboard
```

## Setup (≈20 min, one time)

### 1. Ignav API key (instant, free to start)
1. Sign up at https://ignav.com → **Get API key** (no credit card).
2. First 1,000 requests are free; after that $2 per 1,000, no monthly minimum.
   This config uses ~600 searches/month plus one booking-link lookup per alert,
   so expect roughly **$1.30/month** once the free credit is spent. Only
   successful (HTTP 200) requests are billed.

### 2. Repo
1. Create a new GitHub repo (private is fine) and push these files.
2. Settings → Pages → Source: **Deploy from a branch** → branch `main`, folder `/docs`.
   Your dashboard URL: `https://<user>.github.io/<repo>/`.
3. Settings → Secrets and variables → Actions → add:

| Secret | Value |
|---|---|
| `IGNAV_API_KEY` | from step 1 |
| `SMTP_HOST` / `SMTP_PORT` | e.g. `smtp.gmail.com` / `465` |
| `SMTP_USER` / `SMTP_PASS` | Gmail address + [App Password](https://myaccount.google.com/apppasswords) |
| `ALERT_EMAIL_TO` | where alerts go |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | free, via @BotFather — no Twilio needed |
| `TWILIO_SID` / `TWILIO_TOKEN` | optional, from https://console.twilio.com |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` (Twilio sandbox number) |
| `WHATSAPP_TO` | `whatsapp:+1787XXXXXXX` (your number) |

Channels are optional — the radar runs with any subset of secrets; missing
channels are skipped silently. Telegram is the free-forever path; for WhatsApp,
join the Twilio sandbox once by sending the join code it shows you.

### 3. First run
Actions tab → **SJU Fare Radar** → *Run workflow*. When it finishes, refresh your
dashboard. From then on it runs itself daily at ~08:00 Puerto Rico time.

## Tuning

- **Routes & thresholds:** edit `config.yaml`. Thresholds are seeded from the
  strategy MD's verified benchmarks (Madrid $620, Bogotá $200, BWI $160...).
- **API budget:** `samples_per_run: 2` × 10 routes × 1 run/day ≈ 600 searches/month.
  Raise samples or cron frequency knowing each +1 sample adds ~300 requests/month
  (~$0.60).
- **Trip length:** `trip_length_days` sets the RT window sampled (default 7).
- **Alert logic:** fires when price ≤ `alert_below` OR a new observed floor is set,
  with a 24 h cooldown per route unless the price keeps dropping.

## Honest limits (by design)

- Ignav fares carry a `status` field; alerts marked **CONFIRMED** were
  checkout-grade at scan time. Either way, **the price at your checkout screen
  is the only guarantee** — which is why every alert links to live fares
  instead of quoting a cached page.
- GitHub Actions cron can drift ±15 min; irrelevant at 1 run/day.
- Split-ticket builds are first-class: `split_legs` can set a per-leg `origin`
  (e.g. LAX→NRT for Zipair, MAD→PRG for LCC hops), and `builds` sums member
  legs into one tracked price with its own threshold, floor, and alerts — shown
  on the dashboard against the direct fare. Build legs are separate tickets:
  self-transfer risk is yours; book per the strategy MD's Step 5a and the DOT
  24-hour rule. Leg prices are sampled on matching dates but cheapest-of-run,
  so treat a build alert as a signal to line up real dates, not a quote.
