"""SJU Fare Radar — live fare checker (provider-agnostic).

Scans every route in config.yaml via the configured fare provider
(default: Ignav; see providers.py), records history, and alerts when a
fare crosses its threshold or sets a new floor. Every result ships with
a link that opens live, bookable prices.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

import baselines
import store
from budget import Budget
from providers import get_provider

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
HISTORY_PATH = ROOT / "docs" / "data" / "history.json"

# history.json points stay lean (dashboard payload); the full observation
# including duration/ignav_id goes to SQLite.
POINT_FIELDS = ("price", "carriers", "link", "confirmed", "stops")


def tier_reason(price: float, stats: dict) -> str:
    pct = round(100 * (price - stats["median"]) / stats["median"])
    return f"${price:.0f} — typically ~${stats['median']:.0f} ({pct:+d}%)"


def summarize_window(anchor: date, anchor_price: float,
                     probes: list[tuple[date, float]],
                     tolerance_pct: float = 15) -> str | None:
    """Going-style date window: which probed dates price within tolerance of
    the alerted fare. Returns copy like 'similar prices Sep 11 – Oct 09' when
    at least one neighbor qualifies, else None."""
    tol = 1 + tolerance_pct / 100
    good = sorted([d for d, p in probes if p <= anchor_price * tol] + [anchor])
    if len(good) < 2:
        return None
    return (f"similar prices {good[0].strftime('%b %d')} – "
            f"{good[-1].strftime('%b %d')} departures "
            f"({len(good)} of {len(probes) + 1} dates checked)")


def persist_offers(conn, origin: str, dest: str, offers: list[dict],
                   depart: date, ret: date | None, source_job: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for offer in offers:
        store.record_observation(conn, {
            **offer,
            "observed_at": now,
            "origin": origin,
            "destination": dest,
            "trip_type": "round_trip" if ret else "one_way",
            "depart_date": depart.isoformat(),
            "return_date": ret.isoformat() if ret else None,
            "carrier": "/".join(offer.get("carriers") or []) or None,
            "source_job": source_job,
        })


def rake_dates(today: date, n: int, lo: int, hi: int, stride: int = 5) -> list[date]:
    """Rotating calendar rake. The booking curve [lo, hi) days out is split
    into n strata; each daily run prices a different offset inside every
    stratum, advancing `stride` days per run. stride=5 is coprime with 7, so
    departure weekdays rotate too. Coarse coverage of the whole curve lands
    within ~stratum/stride runs; full daily granularity within one stratum
    cycle. Deterministic in `today` — no state to persist."""
    span = max(hi - lo, n)
    stratum = max(span // n, 1)
    walk = (today.toordinal() * stride) % stratum
    return [today + timedelta(days=lo + i * stratum + walk) for i in range(n)]


def sample_departure_dates() -> list[date]:
    s = CONFIG["settings"]
    return rake_dates(date.today(), max(1, s["samples_per_run"]),
                      s.get("rake_min_days", 14), s["scan_horizon_days"])


def load_history() -> dict:
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text())
    return {"routes": {}, "alerts": []}


def should_alert(entry: dict, price: float, threshold: float) -> str | None:
    floor = entry.get("floor")
    cooldown = timedelta(hours=CONFIG["settings"]["alert_cooldown_hours"])
    last = entry.get("last_alert")
    if last:
        last_dt = datetime.fromisoformat(last["at"])
        if (datetime.now(timezone.utc) - last_dt) < cooldown and price >= last["price"]:
            return None
    if price <= threshold:
        return f"below your ${threshold:.0f} threshold"
    if floor is not None and price < floor:
        return f"new all-time observed floor (was ${floor:.2f})"
    return None


def run() -> None:
    s = CONFIG["settings"]
    conn = store.connect()
    budget = Budget(conn, CONFIG.get("budget"))
    provider = get_provider(s.get("provider", "ignav"), counter=budget.counter)
    history = load_history()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pending = []
    thresholds = {}

    region_of = {code: reg for reg, codes in CONFIG.get("regions", {}).items()
                 for code in codes}

    run_best = {}
    win_cfg = CONFIG.get("window", {})
    windows_probed = 0
    war_triggers = []   # (code, depart_iso, return_iso) of hot/mistake routes
    tagged = ([(r, "destination") for r in CONFIG["routes"]] +
              [(r, "positioning" if r.get("origin", s["origin"]) == s["origin"]
                   else "connector") for r in CONFIG.get("split_legs", [])])
    for route, kind in tagged:
        code, label = route["code"], route["label"]
        origin = route.get("origin", s["origin"])
        key = route.get("key") or (code if origin == s["origin"] else f"{origin}→{code}")
        thresholds[key] = route["alert_below"]
        # Buffer support: connecting legs depart offset days after the base date
        # (overnight self-transfer), positioning legs span a longer round trip.
        offset = timedelta(days=route.get("depart_offset_days", 0))
        leg_trip = timedelta(days=route.get("trip_days", s["trip_length_days"]))
        print(f"Scanning {origin} -> {code} ({label})")
        # Flex-date grid: a route with target_depart scans target ±flex_days
        # instead of the horizon samples, so neighboring dates can beat it.
        flex_target = route.get("target_depart") if route.get("flex", True) else None
        if isinstance(flex_target, str):
            flex_target = date.fromisoformat(flex_target)
        if flex_target:
            flex_days = int(route.get("flex_days", s.get("flex_days", 3)))
            bases = [(flex_target + timedelta(days=d), "flex" if d else "watch")
                     for d in range(-flex_days, flex_days + 1)]
        else:
            bases = [(b, "watch") for b in sample_departure_dates()]
        results = []
        for base, job in bases:
            depart = base + offset
            provider.job = job
            offers = provider.search(origin, code, depart, depart + leg_trip, s)
            if offers:
                persist_offers(conn, origin, code, offers,
                               depart, depart + leg_trip, job)
                best_of_date = dict(offers[0])
                best_of_date.update({"depart": depart.isoformat(),
                                     "return": (depart + leg_trip).isoformat()})
                results.append(best_of_date)
        provider.job = "watch"
        if not results:
            continue
        flex_note = None
        if flex_target:
            target_iso = (flex_target + offset).isoformat()
            on_target = next((r for r in results if r["depart"] == target_iso), None)
            neighbors = [r for r in results if r["depart"] != target_iso]
            beat = s.get("flex_beat_pct", 15) / 100
            if on_target and neighbors:
                nb = min(neighbors, key=lambda r: r["price"])
                if nb["price"] <= on_target["price"] * (1 - beat):
                    nb_day = date.fromisoformat(nb["depart"])
                    flex_note = (f"${on_target['price'] - nb['price']:.0f} less "
                                 f"departing {nb_day.strftime('%a %b %d')} "
                                 f"(${nb['price']:.0f} vs ${on_target['price']:.0f} "
                                 f"on your target date)")
        best = min(results, key=lambda r: r["price"])
        ignav_id = best.get("ignav_id")
        self_transfer = best.get("self_transfer", False)
        best = {f: best[f] for f in (*POINT_FIELDS, "depart", "return")}
        if self_transfer:
            best["self_transfer"] = True
        run_best[key] = dict(best)

        entry = history["routes"].setdefault(
            key, {"label": label, "floor": None, "points": [], "last_alert": None})
        entry["label"] = label
        entry["origin"] = origin
        entry["kind"] = kind
        entry["region"] = region_of.get(code, "other")
        entry["points"].append({"at": now, **best})
        entry["points"] = entry["points"][-500:]

        # Tiered alerts against the route baseline; static threshold + floor
        # logic remains the fallback until the route has enough history.
        bl_cfg = CONFIG.get("baselines", {})
        stats = baselines.route_stats(conn, origin, code, "round_trip", bl_cfg)
        tier = reason = None
        if stats["ready"]:
            tier = baselines.classify(best["price"], stats, bl_cfg)
            if tier:
                if baselines.should_send(conn, key, tier, best["price"], bl_cfg):
                    reason = tier_reason(best["price"], stats)
                else:
                    print(f"  {tier} ${best['price']} — in cooldown, skipped")
                    tier = None
        else:
            reason = should_alert(entry, best["price"], route["alert_below"])
            tier = "legacy" if reason else None
        if entry["floor"] is None or best["price"] < entry["floor"]:
            entry["floor"] = best["price"]
        if tier:
            # Alert-worthy: spend one extra billable call on an airline-direct link.
            if ignav_id and not budget.exhausted and hasattr(provider, "booking_link"):
                direct = provider.booking_link(ignav_id)
                if direct:
                    best["link"] = direct
            alert = {"at": now, "route": key, "label": label, "origin": origin,
                     "flex_note": flex_note,
                     "price": best["price"], "reason": reason, "tier": tier,
                     "typical": stats["median"] if stats["ready"] else None,
                     "self_transfer": bool(best.get("self_transfer")),
                     "confirmed": bool(best.get("confirmed")),
                     "depart": best["depart"], "return": best["return"],
                     "stops": best.get("stops"),
                     "carriers": best["carriers"], "link": best["link"]}
            entry["last_alert"] = {"at": now, "price": best["price"]}
            store.record_alert(conn, key, tier, best["price"], now)
            # Date-window probe: price ±1/±2 weeks around the alerted depart
            # so the alert reports a Going-style travel window, not one date.
            if (win_cfg.get("enabled", True) and not flex_target
                    and windows_probed < win_cfg.get("max_alerts_per_run", 3)
                    and not budget.exhausted):
                windows_probed += 1
                anchor = date.fromisoformat(best["depart"])
                probes = []
                provider.job = "flex"
                for off in win_cfg.get("offsets_days", [-14, -7, 7, 14]):
                    d = anchor + timedelta(days=off)
                    if d <= date.today():
                        continue
                    offers = provider.search(origin, code, d, d + leg_trip, s)
                    if offers:
                        persist_offers(conn, origin, code, offers,
                                       d, d + leg_trip, "flex")
                        probes.append((d, offers[0]["price"]))
                provider.job = "watch"
                alert["window_note"] = summarize_window(
                    anchor, best["price"], probes,
                    win_cfg.get("tolerance_pct", 15))
            if tier in ("hot", "mistake") and kind == "destination":
                war_triggers.append((code, best["depart"], best["return"]))
            history["alerts"] = ([alert] + history.get("alerts", []))[:100]
            pending.append(alert)
            print(f"  ALERT [{tier}]: ${best['price']} — {reason}")
        else:
            print(f"  best ${best['price']} ({best['depart']})")

    # Fare-war propagation: a hot/mistake fare on one route often means the
    # whole region is on sale (Going's experts' heuristic). Probe configured
    # siblings on the same dates; survivors of the tier engine alert too.
    war_cfg = CONFIG.get("fare_war", {})
    if war_triggers and war_cfg.get("enabled", True) and not budget.exhausted:
        bl_cfg = CONFIG.get("baselines", {})
        spent_before_war = budget.spent
        provider.job = "war"
        probed = set()
        for code, dep_iso, ret_iso in war_triggers:
            for sib in war_cfg.get("siblings", {}).get(code, [])[:war_cfg.get("max_siblings", 3)]:
                if (sib in thresholds or sib in probed or budget.exhausted
                        or budget.spent - spent_before_war >= war_cfg.get("max_requests_per_run", 12)):
                    continue
                probed.add(sib)
                dep, ret = date.fromisoformat(dep_iso), date.fromisoformat(ret_iso)
                offers = provider.search(s["origin"], sib, dep, ret, s)
                if not offers:
                    continue
                persist_offers(conn, s["origin"], sib, offers, dep, ret, "war")
                price = offers[0]["price"]
                sstats = baselines.route_stats(conn, s["origin"], sib, "round_trip", bl_cfg)
                stier = baselines.classify(price, sstats, bl_cfg)
                if stier and baselines.should_send(conn, sib, stier, price, bl_cfg):
                    store.record_alert(conn, sib, stier, price, now)
                    pending.append({
                        "at": now, "route": sib, "label": f"{sib} (fare-war check, {code} is on sale)",
                        "origin": s["origin"], "price": price, "tier": stier,
                        "reason": tier_reason(price, sstats),
                        "typical": sstats["median"],
                        "self_transfer": bool(offers[0].get("self_transfer")),
                        "confirmed": bool(offers[0].get("confirmed")),
                        "depart": dep_iso, "return": ret_iso,
                        "stops": offers[0].get("stops"),
                        "carriers": offers[0]["carriers"], "link": offers[0]["link"]})
                    print(f"  fare-war {code}→{sib}: ALERT [{stier}] ${price}")
                else:
                    print(f"  fare-war {code}→{sib}: ${price} banked")
        provider.job = "watch"

    # Positioning comparison: on configured weekdays, price the through
    # itinerary against SJU↔hub + hub↔destination splits (RT legs with the
    # repo's overnight self-transfer buffer) for flagged long-haul routes.
    pos_cfg = CONFIG.get("positioning", {})
    pos_notes: dict[str, dict] = {}
    if (pos_cfg.get("enabled")
            and datetime.now(timezone.utc).weekday() in pos_cfg.get("run_weekdays", [0, 3])
            and not budget.exhausted):
        provider.job = "positioning"
        for route in CONFIG["routes"]:
            code = route["code"]
            through = run_best.get(code)
            if not route.get("positioning_check") or not through:
                continue
            depart = date.fromisoformat(through["depart"])
            ret = date.fromisoformat(through["return"])
            best_split = None
            for hub in pos_cfg.get("hubs", ["MCO", "FLL", "JFK", "BOS"]):
                if budget.exhausted:
                    break
                pos_offers = provider.search(s["origin"], hub, depart,
                                             ret + timedelta(days=2), s)
                conn_offers = provider.search(hub, code, depart + timedelta(days=1),
                                              ret + timedelta(days=1), s)
                if pos_offers:
                    persist_offers(conn, s["origin"], hub, pos_offers,
                                   depart, ret + timedelta(days=2), "positioning")
                if conn_offers:
                    persist_offers(conn, hub, code, conn_offers,
                                   depart + timedelta(days=1),
                                   ret + timedelta(days=1), "positioning")
                if not (pos_offers and conn_offers):
                    continue
                total = round(pos_offers[0]["price"] + conn_offers[0]["price"], 2)
                if best_split is None or total < best_split["total"]:
                    best_split = {"hub": hub, "total": total,
                                  "pos": pos_offers[0]["price"],
                                  "conn": conn_offers[0]["price"]}
            ratio = pos_cfg.get("beat_ratio", 0.80)
            if best_split and best_split["total"] < through["price"] * ratio:
                note = (f"Split via {best_split['hub']}: ${best_split['total']:.0f} "
                        f"(SJU↔{best_split['hub']} ${best_split['pos']:.0f} + "
                        f"{best_split['hub']}↔{code} ${best_split['conn']:.0f}) vs "
                        f"${through['price']:.0f} through — separate tickets, no "
                        f"protection on missed connections, leave a 4h+ buffer.")
                pos_notes[code] = {"note": note, "total": best_split["total"]}
                print(f"  positioning {code}: split ${best_split['total']:.0f} "
                      f"beats through ${through['price']:.0f}")
        provider.job = "watch"
        # Attach to this run's alerts; anything left is worth its own message.
        for alert in pending:
            if alert["route"] in pos_notes:
                alert["positioning_note"] = pos_notes.pop(alert["route"])["note"]
        for code, found in pos_notes.items():
            if baselines.should_send(conn, code, "positioning", found["total"],
                                     CONFIG.get("baselines", {})):
                store.record_alert(conn, code, "positioning", found["total"], now)
                from alerts import send_telegram_text
                send_telegram_text(f"🧩 Positioning find — SJU→{code}\n{found['note']}")

    # Split builds: combined legs vs the direct single ticket.
    build_thresholds = {}
    for build in CONFIG.get("builds", []):
        name, legs = build["name"], build["legs"]
        build_thresholds[name] = build["alert_below"]
        missing = [l for l in legs if l not in run_best]
        if missing:
            print(f"Build {name}: no data for {missing}, skipped")
            continue
        combined = round(sum(run_best[l]["price"] for l in legs), 2)
        direct = run_best.get(build.get("versus"), {}).get("price")

        entry = history.setdefault("builds", {}).setdefault(
            name, {"floor": None, "points": [], "last_alert": None})
        final_dest = (build.get("versus") or legs[-1]).split("→")[-1].rstrip("+")
        entry.update({"legs": legs, "versus": build.get("versus"),
                      "region": region_of.get(final_dest, "other")})
        entry["points"].append({"at": now, "price": combined, "direct": direct})
        entry["points"] = entry["points"][-500:]

        reason = should_alert(entry, combined, build["alert_below"])
        if entry["floor"] is None or combined < entry["floor"]:
            entry["floor"] = combined
        vs = f" vs ${direct:.0f} direct" if direct else ""
        suppressed = False
        if reason and direct is not None and combined >= direct:
            # A build that doesn't beat the single ticket isn't actionable.
            print(f"Build {name}: ${combined}{vs} — {reason}, "
                  f"suppressed (direct is cheaper)")
            reason, suppressed = None, True
        if reason:
            first = run_best[legs[0]]
            alert = {"at": now, "route": name, "label": name, "origin": s["origin"],
                     "tier": "legacy", "self_transfer": True,
                     "price": combined, "reason": reason + vs,
                     "confirmed": all(run_best[l].get("confirmed") for l in legs),
                     "depart": first["depart"], "return": first["return"],
                     "carriers": sorted({c for l in legs for c in run_best[l]["carriers"]}),
                     "link": first["link"],
                     "breakdown": " + ".join(f"{l} ${run_best[l]['price']:.0f}" for l in legs),
                     "leg_links": {l: run_best[l]["link"] for l in legs}}
            entry["last_alert"] = {"at": now, "price": combined}
            history["alerts"] = ([alert] + history.get("alerts", []))[:100]
            pending.append(alert)
            print(f"Build {name}: ALERT ${combined}{vs} — {reason}")
        elif not suppressed:
            print(f"Build {name}: ${combined}{vs}")

    # Drop history for routes/builds removed from config so the dashboard
    # doesn't show permanently stale rows.
    history["routes"] = {k: v for k, v in history["routes"].items() if k in thresholds}
    if "builds" in history:
        history["builds"] = {k: v for k, v in history["builds"].items()
                             if k in build_thresholds}

    # Dashboard payload: per-route baseline stats + a 60-day daily-min spark
    # series, computed from SQLite at build time.
    bl_cfg = CONFIG.get("baselines", {})
    spark_cutoff = (date.today() - timedelta(days=60)).isoformat()
    for key, entry in history["routes"].items():
        origin_e = key.split("→")[0] if "→" in key else entry.get("origin", s["origin"])
        dest_e = (key.split("→")[-1] if "→" in key else key).rstrip("+")
        rstats = baselines.route_stats(conn, origin_e, dest_e, "round_trip", bl_cfg)
        latest = entry["points"][-1] if entry.get("points") else None
        delta = (round(100 * (latest["price"] - rstats["median"]) / rstats["median"])
                 if latest and rstats["median"] else None)
        entry["stats"] = {"median": rstats["median"], "p25": rstats["p25"],
                          "p10": rstats["p10"], "n": rstats["n_observations"],
                          "ready": rstats["ready"], "delta_pct": delta}
        entry["spark60"] = [r["p"] for r in conn.execute(
            "SELECT substr(observed_at, 1, 10) AS d, MIN(price) AS p "
            "FROM fare_observations WHERE origin = ? AND destination = ? "
            "AND trip_type = 'round_trip' AND observed_at >= ? "
            "GROUP BY d ORDER BY d", (origin_e, dest_e, spark_cutoff))]

    history["updated_at"] = now
    history["thresholds"] = thresholds
    history["build_thresholds"] = build_thresholds
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=1))
    budget.check_and_notify()
    print(f"Budget: {budget.status_line()}")
    conn.commit()
    conn.close()

    if pending:
        from alerts import dispatch
        dispatch(pending)


if __name__ == "__main__":
    run()
