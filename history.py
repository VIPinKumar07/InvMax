#!/usr/bin/env python3
"""
InvMax History Accumulator  (Layer 1.5 - rolling time-series store)
====================================================================
Runs AFTER fetch_data.py, BEFORE signals.py. Appends today's snapshot of the
POINT-IN-TIME fields (things the site otherwise only sees as "today's number")
into rolling per-field series so we can see how they trended over the last
6 months (180 days by default). Deterministic, plain JSON, git-committed.

Why not a database:
  * Zero infra: no server to run, secure, or pay for.
  * Auditable: every daily update is a git commit; you can diff back in time.
  * Tiny footprint: 180 days x ~15 fields is <1 MB.
  * Reproducible: same input -> same output. No hidden state.

What it does NOT do:
  * It does NOT touch price series (nifty/sensex/gold/brent/sectors/etc.) -
    those already carry ~1yr of daily closes from Yahoo per fetch, so a
    rolling snapshot would be redundant.
  * It ONLY builds series for fields that are otherwise a single number:
    fx (USD/INR + majors), rates (10Y, repo, US 10Y), macro (CPI/GDP/reserves),
    flows (FII/DII), and the signal-engine's overall regime score.

Files it maintains (all under data/history/):
  fx.json       - USD/INR + majors, daily
  rates.json    - India 10Y + repo + US 10Y, daily
  macro.json    - CPI, GDP, reserves (only appends when the World-Bank period
                  changes, so lagged annuals don't clutter the series)
  flows.json    - FII/DII daily net (Cr)
  regime.json   - overall_regime score + label + as-of (from signals block)

Shape of each file:
  {
    "field_id": {
      "unit": "%|Cr|INR|score|...",
      "source": "RBI MPC" | "ECB / Frankfurter" | ...,
      "series": [
        {"date": "2026-08-04", "value": 5.50},
        ...
      ]
    },
    ...
  }

Run locally:  python history.py
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = os.environ.get("INVMAX_OUT", "data")
LATEST = os.path.join(OUT_DIR, "latest.json")
HIST_DIR = os.path.join(OUT_DIR, "history")

WINDOW_DAYS = int(os.environ.get("INVMAX_HISTORY_DAYS", "180"))

log = lambda *a: print("[history]", *a, flush=True)


# ─────────────────────────────────────────────────────────────
# IO helpers
# ─────────────────────────────────────────────────────────────
def today_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")


def load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def prune(series, days):
    """Keep only points within the last `days` days (based on date field)."""
    if not series:
        return series
    cutoff = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
    return [p for p in series if p.get("date", "") >= cutoff]


def upsert(series, date, value, extra=None):
    """Add or replace today's point. Keeps the series sorted, one entry per date."""
    if value is None:
        return series
    out = [p for p in series if p.get("date") != date]
    row = {"date": date, "value": value}
    if extra:
        row.update(extra)
    out.append(row)
    out.sort(key=lambda p: p.get("date", ""))
    return out


def ensure_field(store, key, unit, source):
    """Get or create a field record with metadata."""
    if key not in store:
        store[key] = {"unit": unit, "source": source, "series": []}
    else:
        # keep metadata fresh
        store[key]["unit"] = unit
        store[key]["source"] = source
    return store[key]


# ─────────────────────────────────────────────────────────────
# Per-domain snapshots
# ─────────────────────────────────────────────────────────────
def snap_fx(blocks, date):
    """FX: today's USD/INR (+ majors) → history/fx.json."""
    path = os.path.join(HIST_DIR, "fx.json")
    store = load(path, {})
    fx = ((blocks or {}).get("fx") or {}).get("data") or {}
    rates = fx.get("rates") or {}
    n = 0
    for ccy in ("INR", "EUR", "GBP", "JPY"):
        v = rates.get(ccy)
        if v is None:
            continue
        rec = ensure_field(store, ccy, "per USD", "ECB / Frankfurter")
        rec["series"] = prune(upsert(rec["series"], date, round(float(v), 4)), WINDOW_DAYS)
        n += 1
    save(path, store)
    log(f"  fx.json: {n} currencies · INR series len {len(store.get('INR', {}).get('series', []))}")


def snap_rates(blocks, date):
    """Rates: India 10Y + repo (registry values), US 10Y (fetched)."""
    path = os.path.join(HIST_DIR, "rates.json")
    store = load(path, {})
    rates = ((blocks or {}).get("rates") or {}).get("data") or {}
    for rid, meta_source in (("gsec_10y", "RBI / FBIL"),
                             ("policy_repo", "RBI MPC"),
                             ("us_10y", "Yahoo Finance")):
        r = rates.get(rid) or {}
        v = r.get("last") if r.get("last") is not None else r.get("value")
        if v is None:
            continue
        rec = ensure_field(store, rid, "%", r.get("source", meta_source))
        extra = {}
        if r.get("as_of") and not r.get("last"):
            extra["as_of"] = r["as_of"]  # for registry values, remember the maintained date
        rec["series"] = prune(upsert(rec["series"], date, round(float(v), 4), extra), WINDOW_DAYS)
    save(path, store)
    log(f"  rates.json: {len(store)} fields · 10Y series len {len(store.get('gsec_10y', {}).get('series', []))}")


def snap_macro(blocks, date):
    """Macro (CPI/GDP/reserves): only append when the underlying PERIOD changes,
    so a lagged annual value doesn't create 180 duplicate rows."""
    path = os.path.join(HIST_DIR, "macro.json")
    store = load(path, {})
    macro = ((blocks or {}).get("macro") or {}).get("data") or {}
    for mid, unit in (("cpi_inflation_yoy", "%"),
                      ("gdp_growth", "%"),
                      ("fx_reserves_usd", "USD")):
        m = macro.get(mid)
        if not m or m.get("value") is None:
            continue
        rec = ensure_field(store, mid, unit, "World Bank via DBnomics")
        prev = rec["series"][-1] if rec["series"] else None
        # append only if the source period is new (else this is the same annual figure)
        if prev and prev.get("period") == m.get("period"):
            continue
        rec["series"].append({
            "date": date,                # when we captured it
            "period": m.get("period"),   # what period the source is reporting
            "value": float(m["value"]),
        })
        # keep at most 20 historical periods per macro field
        rec["series"] = rec["series"][-20:]
    save(path, store)
    log(f"  macro.json: {len(store)} fields")


def snap_flows(blocks, date):
    """FII/DII daily flows → the most valuable historical series to build,
    because today's flow means little; the trend over weeks does."""
    path = os.path.join(HIST_DIR, "flows.json")
    store = load(path, {})
    flows = ((blocks or {}).get("flows") or {}).get("data") or {}
    for fid, name in (("fii_net", "FII net (equity)"),
                      ("dii_net", "DII net (equity)")):
        v = flows.get(fid)
        if v is None:
            # some feeds nest as fii/dii → {net,...}
            nested = flows.get(fid.split("_")[0])
            if isinstance(nested, dict):
                v = nested.get("net")
        if v is None:
            continue
        rec = ensure_field(store, fid, "Cr", "NSE FII/DII")
        rec["name"] = name
        rec["series"] = prune(upsert(rec["series"], date, round(float(v), 2)), WINDOW_DAYS)
    save(path, store)
    log(f"  flows.json: FII len {len(store.get('fii_net', {}).get('series', []))} · "
        f"DII len {len(store.get('dii_net', {}).get('series', []))}")


def snap_regime(blocks, date):
    """The overall observed-regime score, so we can chart 'when did the market
    lean flip risk-on/risk-off' over the last 6 months."""
    path = os.path.join(HIST_DIR, "regime.json")
    store = load(path, {})
    sig = (blocks or {}).get("signals") or {}
    reg = sig.get("regime") or {}
    if reg.get("score") is None:
        return
    rec = ensure_field(store, "overall", "score", "InvMax signal engine")
    rec["series"] = prune(upsert(
        rec["series"], date, float(reg["score"]),
        extra={"label": reg.get("label")}
    ), WINDOW_DAYS)
    save(path, store)
    log(f"  regime.json: series len {len(rec['series'])}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    if not os.path.exists(LATEST):
        log(f"No {LATEST}; run fetch_data.py first. Nothing to accumulate.")
        return 0
    try:
        with open(LATEST, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"Bad {LATEST}: {type(e).__name__}: {e}")
        return 0

    blocks = data.get("blocks") or {}
    date = today_ist()

    log(f"Snapshotting {date} into {HIST_DIR}/  (window: {WINDOW_DAYS} days)")
    snap_fx(blocks, date)
    snap_rates(blocks, date)
    snap_macro(blocks, date)
    snap_flows(blocks, date)
    snap_regime(blocks, date)

    # Small manifest so the frontend can discover what's stored without listing files.
    manifest = {
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "window_days": WINDOW_DAYS,
        "files": sorted([f for f in os.listdir(HIST_DIR) if f.endswith(".json") and f != "manifest.json"]),
    }
    save(os.path.join(HIST_DIR, "manifest.json"), manifest)
    log(f"WROTE manifest with {len(manifest['files'])} files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
