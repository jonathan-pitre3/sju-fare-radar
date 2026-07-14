# ✈️ SJU Fare Radar

Your personal Google Flights-style price watcher for flights departing **San Juan (SJU)**.
Live fares via the Ignav API, scanned automatically by GitHub Actions, displayed on a
personal dashboard (GitHub Pages), with Telegram / email / WhatsApp alerts. Every fare
observation is persisted to SQLite, so alerts are tiered against each route's **own
price history** — not just a static threshold — and the radar also *discovers* deals on
~50 destinations you aren't explicitly watching.

Companion to `sju-cheap-flight-search-strategy.md`, which seeds the thresholds and
holds the routing heuristics (positioning hubs, split-ticket rules, booking guide).

> Provider history: originally built on Amadeus Self-Service, which shut down
> (keys die 2026-07-17). The provider layer is swappable (`providers.py`);
> Ignav is the default and the Amadeus adapter is kept as legacy reference.

## Architecture

```
GitHub Actions
├─ radar.yml     daily ~08:00 PR   fare_radar/check_fares.py
│    watched routes (+ flex ±3d grids, Mon/Thu positioning splits)
│    ├─ data/fares.db            every observation (SQLite, committed back)
│    ├─ baselines.py             120-day median / p25 / p10 per route
│    ├─ tier engine              💰 deal · 🔥 hot · 🚨 possible mistake fare
│    ├─ docs/data/history.json   dashboard payload (stats + 60-day sparks)
│    └─ alerts.py                Telegram + email + WhatsApp
├─ weekly.yml    Saturdays        explore.py (48-destination one-way sweep)
│                                 market_check.py (point-of-sale probe)
└─ commands.yml  every 2h         telegram_commands.py (/historial, /presupuesto)

GitHub Pages serves docs/ → your dashboard
```

The SQLite database (`data/fares.db`) is the system of record: fare observations,
alert cooldowns, and the monthly request-budget ledger. Workflows commit it back
with `[skip ci]`. `docs/data/history.json` remains the dashboard's data file.

## How the tiers work

Each route's baseline is computed over a rolling **120-day window**, grouped by
`(origin, destination, trip type)` — the baseline describes the route, not one
travel date. Percentile tiers activate once a route has **≥ 20 observations
spanning ≥ 21 days**; until then the static `alert_below` threshold + observed
floor logic applies (the message copy doesn't change, only the trigger).

| Tier | Fires when | Behavior |
|---|---|---|
| 💰 Deal | price < p25 | Included in the normal alert digest |
| 🔥 Hot deal | price < p10 | Immediate, distinct formatting |
| 🚨 Possible mistake fare | price < 45% of median **and** n ≥ 30 | Immediate, urgent formatting, DOT 24-hour free-cancellation reminder |

Every alert shows the price against typical (`$312 — typically ~$540 (-42%)`),
carrier, stops, and a booking link (airline-direct when Ignav can build one —
fetched only for alert-worthy fares, since those lookups bill). The same
(route, tier) won't re-alert unless the price drops a further **≥ 8%** or
**72 hours** pass.

## The jobs

- **Watch (daily).** Every route/leg/build in `config.yaml`, top-3 offers per
  sampled date persisted. Departure dates come from a **rotating calendar
  rake**: each run prices a different slice of the 2–9-month booking curve
  (weekday-rotating), so every route sees the whole calendar every ~3–4
  weeks at constant request cost. When an alert fires, a **date-window
  probe** prices ±1/±2 weeks so the alert reports a travel window
  ("similar prices Sep 11 – Oct 09"), and **fare-war propagation** probes
  the fired route's regional siblings on the same dates — siblings that
  clear the tier engine alert too. Routes with `target_depart` scan the
  **flex grid** (±`flex_days`, default 3) and alerts mention when a
  neighbor date beats the target by ≥ 15%.
- **Wide net (daily, needs `TRAVELPAYOUTS_TOKEN`).** A Going-style
  two-stage funnel: free cached Aviasales calendars (per-day prices, months
  ahead) surface candidate dates priced under a route's own p25; only the
  best candidates get a billable live Ignav verification (≤ 10/run), and
  only live-verified fares persist or alert. Cached prices never touch the
  baselines. Skips silently until the secret is set. On Mon/Thu, routes flagged `positioning_check` also price
  SJU↔hub + hub↔destination splits (hubs `MCO/FLL/JFK/BOS`, round-trip legs
  with the repo's overnight self-transfer buffer); a split under 80% of the
  through fare is called out — with an explicit separate-tickets warning.
- **Explore (weekly).** One-way discovery across ~48 destinations, three
  goldilocks dates each (Tue/Wed departures; short-haul ~5/9/13 weeks out,
  long-haul ~9/18/30). Hot/mistake finds alert immediately; everything else
  arrives as one "Weekly radar from SJU" digest. Hard cap: 160 requests/run.
- **Market check (weekly).** Rotating subset of 8 watched routes re-queried
  with Ignav's `market` set to the destination country vs `US`, both in USD.
  A local fare ≥ 15% cheaper is flagged as a point-of-sale discrepancy.
- **Telegram commands (every 2h).** `/historial [ORIGIN] DEST` → route stats
  (n, median/p25/p10, best fare ever seen, whether tiers are active);
  `/presupuesto` → request-budget status. Replies arrive on the next poll,
  not instantly (no server by design).

## Request budget

A monthly counter in SQLite mirrors Ignav's billing (only successful HTTP 200
requests count; 424s and network errors retry with backoff and never bill;
empty results are valid, billable responses). Defaults: **cap 4,000/month**
(≈ $8; first 1,000 free, one-time), warning to Telegram at **80%**, and at
100% the explore/flex/positioning/market jobs pause until next month — the
daily watch keeps running so baselines never go blind.

Rough monthly spend with defaults: watch ~1,950 + explore ~620 + positioning
~140 + market ~70 ≈ **2,800 requests ≈ $5.60** (before alert booking-link
lookups, ~1 each).

## Config reference (`config.yaml`)

| Section | What it controls |
|---|---|
| `settings` | origin, currency, trip length, samples per run, provider, `allow_self_transfer`, `flex_days`, `flex_beat_pct` |
| `baselines` | window, tier activation gates, percentiles, mistake-fare guard, cooldowns |
| `budget` | `monthly_request_cap`, `warn_at_pct` |
| `explore` | destination lists (short/long haul), weeks-out triplets, per-run request cap, digest size |
| `positioning` | hub list, run weekdays, beat ratio |
| `market_check` | routes per run, beat %, destination→market map |
| `routes` | watched destinations: `alert_below`, optional `target_depart`/`flex`/`flex_days`, `positioning_check`, `cheap_months` |
| `split_legs` / `builds` | split-ticket ingredients and tracked combinations (see below) |
| `regions` | dashboard color-coding |

## Setup (≈20 min, one time)

### 1. Ignav API key (instant, free to start)
1. Sign up at https://ignav.com → **Get API key** (no credit card).
2. First 1,000 requests are free; after that $2 per 1,000, no monthly minimum.
   Only successful (HTTP 200) requests are billed.

### 2. Repo
1. Create a new GitHub repo (private is fine) and push these files.
2. Settings → Pages → Source: **Deploy from a branch** → branch `main`, folder `/docs`.
   Your dashboard URL: `https://<user>.github.io/<repo>/`.
3. Settings → Secrets and variables → Actions → add:

| Secret | Value |
|---|---|
| `IGNAV_API_KEY` | from step 1 |
| `TRAVELPAYOUTS_TOKEN` | optional — activates the wide net (free token from travelpayouts.com, Profile → API token) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | free, via @BotFather — no Twilio needed |
| `SMTP_HOST` / `SMTP_PORT` | e.g. `smtp.gmail.com` / `465` (optional) |
| `SMTP_USER` / `SMTP_PASS` | Gmail address + [App Password](https://myaccount.google.com/apppasswords) |
| `ALERT_EMAIL_TO` | where alerts go |
| `TWILIO_SID` / `TWILIO_TOKEN` / `TWILIO_WHATSAPP_FROM` / `WHATSAPP_TO` | optional WhatsApp channel |

Channels are optional — the radar runs with any subset of secrets; missing
channels are skipped silently. Telegram is the free-forever path and the only
channel used by the weekly digest, budget warnings, and commands.

### 3. First runs
Actions tab → **SJU Fare Radar** → *Run workflow* (and optionally the weekly
explore). Refresh the dashboard when they finish. From then on everything is
cron-driven.

## Tests

```
python -m unittest discover tests
```

Covers the percentile math, the ≥20-obs/≥21-day activation gates, the
mistake-fare n≥30 false-positive guard, and the 72h/8% cooldown logic.

## Honest limits (by design)

- Ignav fares carry a `status` field; alerts marked **CONFIRMED** were
  checkout-grade at scan time. Either way, **the price at your checkout screen
  is the only guarantee** — which is why every alert links to live fares.
- GitHub Actions cron can drift ±15 min; Telegram command replies wait for the
  next 2-hourly poll.
- Split-ticket builds and positioning splits are separate tickets:
  self-transfer risk is yours; book per the strategy MD's Step 5a and the DOT
  24-hour rule. Leg prices are sampled on matching dates but cheapest-of-run,
  so treat a build alert as a signal to line up real dates, not a quote.
- Market-check observations (both markets) are persisted like any other fare;
  weekly volume is too small to move a 120-day median materially.
- Builds are tracked with the legacy floor/threshold logic (their combined
  price is derived, not an observed itinerary); their member legs get
  percentile baselines individually.
