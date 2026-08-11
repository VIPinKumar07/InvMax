#!/usr/bin/env python3
"""
InvMax Signal Engine  (Layer 3 — observed signals from real data)
=================================================================
Turns the real price series in data/latest.json into DETERMINISTIC, AUDITABLE
signals. This is the layer that replaces invented "conviction scores" with
earned ones: every signal states the rule it applied, the raw reading it saw,
and the conclusion it reached — plus a link to the source so a curious reader
can verify it themselves.

Design contract (from the knowledge-base architecture):
  * A signal is a pure function of data + a stated rule. No LLM, no guessing.
  * Every signal carries: reading (the fact) · conclusion (the hook) ·
    rule (the formula) · direction · strength · source{name,url} · as_of.
  * If history is insufficient, the signal says so — it never fabricates.
  * Deterministic and reproducible: same input → same output, always.

Runs AFTER fetch_data.py, BEFORE synthesize.py. Writes a `signals` block into
data/latest.json that both the site and the narrator read.

Run locally:  python signals.py
"""

import json
import os
import sys
import math
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = os.environ.get("INVMAX_OUT", "data")
OUT_FILE = os.path.join(OUT_DIR, "latest.json")

log = lambda *a: print("[signals]", *a, flush=True)

# Where each asset's series comes from — the source link shown to the reader.
SOURCE = {
    "nifty50":   {"name": "NSE / Stooq EOD",  "url": "https://stooq.com/q/d/?s=^nsei"},
    "sensex":    {"name": "BSE / Stooq EOD",  "url": "https://stooq.com/q/d/?s=^bse"},
    "sp500":     {"name": "Stooq EOD",        "url": "https://stooq.com/q/d/?s=^spx"},
    "gold_usd":  {"name": "Stooq EOD",        "url": "https://stooq.com/q/d/?s=xauusd"},
    "brent_oil": {"name": "Stooq EOD",        "url": "https://stooq.com/q/d/?s=cb.f"},
    "usdinr":    {"name": "ECB / Frankfurter","url": "https://www.frankfurter.app/"},
}
NAME = {
    "nifty50": "Nifty 50", "sensex": "Sensex", "sp500": "S&P 500",
    "gold_usd": "Gold", "brent_oil": "Brent Crude", "usdinr": "USD/INR",
}
# Trading-day lookbacks
LB = {"1M": 21, "3M": 63, "6M": 126, "12M": 252}


# ─────────────────────────────────────────────────────────────
# Pure math helpers (deterministic)
# ─────────────────────────────────────────────────────────────
def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def ret_pct(vals, lookback):
    if len(vals) <= lookback:
        return None
    old = vals[-1 - lookback]
    if not old:
        return None
    return (vals[-1] / old - 1) * 100


def realized_vol(vals, window=63):
    """Annualized realized volatility from daily log returns (%)."""
    if len(vals) < window + 1:
        window = len(vals) - 1
    if window < 5:
        return None
    seg = vals[-window - 1:]
    rets = []
    for i in range(1, len(seg)):
        if seg[i - 1] > 0 and seg[i] > 0:
            rets.append(math.log(seg[i] / seg[i - 1]))
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252) * 100


def max_drawdown(vals, window=252):
    seg = vals[-window:] if len(vals) > window else vals
    if len(seg) < 2:
        return None
    peak = seg[0]
    mdd = 0.0
    for v in seg:
        if v > peak:
            peak = v
        dd = (v / peak - 1) * 100
        if dd < mdd:
            mdd = dd
    return mdd


def pct_from_extreme(vals, window=252, kind="high"):
    seg = vals[-window:] if len(vals) > window else vals
    if not seg:
        return None
    ref = max(seg) if kind == "high" else min(seg)
    if not ref:
        return None
    return (vals[-1] / ref - 1) * 100


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ─────────────────────────────────────────────────────────────
# Per-asset signals
# ─────────────────────────────────────────────────────────────
def asset_signals(key, closes):
    """Return a list of observed signals for one asset. Never fabricates —
    if history is too short, emits an honest 'insufficient history' note."""
    vals = [p["c"] for p in closes if p.get("c") is not None]
    n = len(vals)
    out = []

    if n < 30:
        out.append({
            "id": "coverage", "label": "Data", "direction": "neutral", "strength": 0,
            "reading": f"Only {n} sessions of history available.",
            "conclusion": "Not enough data to read a trend yet.",
            "rule": "needs >= 200 daily closes for full trend signals",
        })
        return out

    last = vals[-1]
    s50, s200 = sma(vals, 50), sma(vals, 200)

    # 1) TREND — the primary, most intuitive signal
    if s200:
        above = (last / s200 - 1) * 100
        golden = s50 and s50 > s200
        if last > s200 and golden:
            concl, dirn = "Uptrend intact", "bullish"
            strength = clamp(55 + above * 2, 55, 92)
        elif last > s200:
            concl, dirn = "Above trend, but momentum mixed", "bullish"
            strength = clamp(50 + above, 50, 75)
        elif last < s200 and s50 and s50 < s200:
            concl, dirn = "Downtrend — below trend on both averages", "bearish"
            strength = clamp(55 + abs(above) * 2, 55, 92)
        else:
            concl, dirn = "Below its long-term average", "bearish"
            strength = clamp(50 + abs(above), 50, 75)
        cross = "50-DMA above 200-DMA" if golden else "50-DMA below 200-DMA"
        out.append({
            "id": "trend", "label": "Trend", "direction": dirn, "strength": round(strength),
            "reading": f"{'+' if above>=0 else ''}{above:.1f}% vs its 200-day average · {cross}.",
            "conclusion": concl,
            "rule": "price vs 200-DMA, and 50-DMA vs 200-DMA (golden/death cross)",
        })
    else:
        out.append({
            "id": "trend", "label": "Trend", "direction": "neutral", "strength": 0,
            "reading": f"{n} sessions — not yet 200 for a full trend read.",
            "conclusion": "Trend forming; needs more history.",
            "rule": "price vs 200-DMA (needs 200 daily closes)",
        })

    # 2) MOMENTUM — multi-horizon return
    r1, r3, r6 = ret_pct(vals, LB["1M"]), ret_pct(vals, LB["3M"]), ret_pct(vals, LB["6M"])
    if r3 is not None:
        if r3 > 8 and (r1 or 0) > 0:
            concl, dirn, strength = "Strong and still rising", "bullish", clamp(55 + r3, 55, 92)
        elif r3 > 0:
            concl, dirn, strength = "Positive but cooling" if (r1 or 0) < 0 else "Positive", "bullish", clamp(50 + r3, 50, 78)
        elif r3 > -8:
            concl, dirn, strength = "Soft", "bearish", clamp(50 + abs(r3), 50, 75)
        else:
            concl, dirn, strength = "Weak — falling hard", "bearish", clamp(55 + abs(r3), 55, 92)
        parts = []
        if r1 is not None: parts.append(f"1M {r1:+.1f}%")
        if r3 is not None: parts.append(f"3M {r3:+.1f}%")
        if r6 is not None: parts.append(f"6M {r6:+.1f}%")
        out.append({
            "id": "momentum", "label": "Momentum", "direction": dirn, "strength": round(strength),
            "reading": " · ".join(parts) + ".",
            "conclusion": concl,
            "rule": "trailing 1M / 3M / 6M price return",
        })

    # 3) POSITION vs 52-week range
    fh = pct_from_extreme(vals, 252, "high")
    fl = pct_from_extreme(vals, 252, "low")
    if fh is not None and fl is not None:
        if fh > -2:
            concl, dirn = "At / near a 1-year high", "bullish"
        elif fl < 3:
            concl, dirn = "Near a 1-year low", "bearish"
        else:
            concl, dirn = "Mid-range", "neutral"
        out.append({
            "id": "range", "label": "52-wk position", "direction": dirn,
            "strength": round(clamp(50 + fh, 20, 95)),
            "reading": f"{fh:+.1f}% from the 1-year high · {fl:+.1f}% above the 1-year low.",
            "conclusion": concl,
            "rule": "distance from trailing 252-day high and low",
        })

    # 4) VOLATILITY regime
    vol = realized_vol(vals, 63)
    if vol is not None:
        if vol < 12:
            concl, dirn = "Calm — low realized volatility", "neutral"
        elif vol < 20:
            concl, dirn = "Normal volatility", "neutral"
        else:
            concl, dirn = "Choppy — elevated volatility", "bearish"
        out.append({
            "id": "vol", "label": "Volatility", "direction": dirn,
            "strength": round(clamp(vol * 2.5, 10, 95)),
            "reading": f"{vol:.0f}% annualized (last 3 months).",
            "conclusion": concl,
            "rule": "annualized std-dev of daily log returns, 63-day window",
        })

    return out


# ─────────────────────────────────────────────────────────────
# Cross-asset signals (the connective, regime-level read)
# ─────────────────────────────────────────────────────────────
def cross_asset_signals(idx_data, fx):
    out = []

    def closes(key):
        a = idx_data.get(key) or {}
        return [p["c"] for p in (a.get("daily_tail") or []) if p.get("c") is not None]

    nifty, gold = closes("nifty50"), closes("gold_usd")

    # 1) EQUITY TREND (Nifty vs 200-DMA) — the anchor
    if len(nifty) >= 200:
        s200 = sma(nifty, 200)
        above = (nifty[-1] / s200 - 1) * 100
        dirn = "bullish" if nifty[-1] > s200 else "bearish"
        out.append({
            "id": "equity_regime", "label": "Equity regime",
            "direction": dirn, "strength": round(clamp(50 + abs(above) * 2, 50, 92)),
            "reading": f"Nifty is {above:+.1f}% versus its 200-day average.",
            "conclusion": "Equities in an uptrend" if dirn == "bullish" else "Equities below trend",
            "rule": "Nifty 50 price vs its 200-day moving average",
            "source": SOURCE["nifty50"],
        })

    # 2) GOLD vs EQUITY — risk appetite tell
    if len(nifty) >= 63 and len(gold) >= 63:
        rn, rg = ret_pct(nifty, 63), ret_pct(gold, 63)
        if rn is not None and rg is not None:
            if rg - rn > 5:
                concl, dirn = "Gold leading equities — defensive tilt", "bearish"
            elif rn - rg > 5:
                concl, dirn = "Equities leading gold — risk appetite healthy", "bullish"
            else:
                concl, dirn = "Gold and equities balanced", "neutral"
            out.append({
                "id": "risk_appetite", "label": "Risk appetite",
                "direction": dirn, "strength": round(clamp(50 + abs(rn - rg) * 3, 50, 90)),
                "reading": f"3-month: equities {rn:+.1f}% vs gold {rg:+.1f}%.",
                "conclusion": concl,
                "rule": "3-month return of Nifty vs Gold (leadership = risk-on/off)",
                "source": SOURCE["gold_usd"],
            })

    # 3) RUPEE direction — imported-inflation / flow tell
    hist = (fx or {}).get("usdinr_history") or []
    vals = [p["v"] for p in hist if p.get("v") is not None]
    if len(vals) >= 21:
        chg = (vals[-1] / vals[-22] - 1) * 100
        if chg > 1:
            concl, dirn = "Rupee weakening — watch imported inflation & FII exit", "bearish"
        elif chg < -1:
            concl, dirn = "Rupee strengthening — supportive backdrop", "bullish"
        else:
            concl, dirn = "Rupee broadly stable", "neutral"
        out.append({
            "id": "rupee", "label": "Rupee",
            "direction": dirn, "strength": round(clamp(50 + abs(chg) * 15, 50, 90)),
            "reading": f"USD/INR moved {chg:+.1f}% over the last month (now {vals[-1]:.2f}).",
            "conclusion": concl,
            "rule": "1-month change in USD/INR",
            "source": SOURCE["usdinr"],
        })

    return out


def build_headline(per_asset, cross):
    """Pick the single most decision-relevant observed signal to lead with."""
    anchor = next((c for c in cross if c["id"] == "equity_regime"), None)
    strongest = None
    pool = list(cross) + [dict(s, _asset=k) for k, v in per_asset.items() for s in v.get("signals", [])]
    for s in pool:
        if s.get("direction") in ("bullish", "bearish") and s.get("strength", 0) >= 70:
            if not strongest or s["strength"] > strongest["strength"]:
                strongest = s
    lead = anchor or strongest
    if not lead:
        return None
    return {
        "label": lead.get("label"),
        "conclusion": lead.get("conclusion"),
        "reading": lead.get("reading"),
        "direction": lead.get("direction"),
        "source": lead.get("source"),
    }


def rates_signals(rates, macro):
    """Observed signals from Indian rates. Yields fall → supportive for duration
    and rate-sensitives; rise → headwind. Real rate = 10y yield − CPI."""
    out = []
    if not rates:
        return out
    g = rates.get("gsec_10y") or {}
    closes = [p["c"] for p in (g.get("daily_tail") or []) if p.get("c") is not None]
    src10 = {"name": "India 10Y · Stooq", "url": "https://stooq.com/q/d/?s=10iny.b"}

    if len(closes) >= 63:
        now = closes[-1]
        chg_3m = now - closes[-63]
        if chg_3m <= -0.25:
            concl, dirn = "Yields falling — supportive for duration & rate-sensitives", "bullish"
        elif chg_3m >= 0.25:
            concl, dirn = "Yields backing up — a headwind for duration", "bearish"
        else:
            concl, dirn = "Yields broadly stable", "neutral"
        out.append({
            "id": "gsec_trend", "label": "10-yr yield",
            "direction": dirn, "strength": round(clamp(50 + abs(chg_3m) * 40, 50, 90)),
            "reading": f"10-yr G-Sec at {now:.2f}% ({chg_3m:+.2f} pts over 3 months).",
            "conclusion": concl,
            "rule": "3-month change in the 10-year G-Sec yield",
            "source": src10,
        })
    elif g.get("last") is not None:
        out.append({
            "id": "gsec_level", "label": "10-yr yield", "direction": "neutral", "strength": 50,
            "reading": f"10-yr G-Sec at {g['last']:.2f}% (building history).",
            "conclusion": "Yield tracked; trend forms as history builds.",
            "rule": "latest 10-year G-Sec yield", "source": src10,
        })

    # Real rate = 10y yield − CPI (if both present)
    cpi = ((macro or {}).get("cpi_inflation_yoy") or {}).get("value")
    if g.get("last") is not None and cpi is not None:
        real = g["last"] - cpi
        if real > 2:
            concl, dirn = "Comfortably positive real rate — supports the rupee & bonds", "bullish"
        elif real > 0:
            concl, dirn = "Mildly positive real rate", "neutral"
        else:
            concl, dirn = "Negative real rate — savers pressured", "bearish"
        out.append({
            "id": "real_rate", "label": "Real rate",
            "direction": dirn, "strength": round(clamp(50 + abs(real) * 12, 50, 88)),
            "reading": f"10-yr yield {g['last']:.2f}% − CPI {cpi:.1f}% = {real:+.1f}% real.",
            "conclusion": concl,
            "rule": "10-year G-Sec yield minus latest CPI inflation",
            "source": src10,
        })

    # Policy stance (repo, registry value)
    repo = rates.get("policy_repo") or {}
    if repo.get("value") is not None:
        out.append({
            "id": "policy", "label": "Policy rate", "direction": "neutral",
            "strength": 50,
            "reading": f"RBI repo held at {repo['value']:.2f}% (as of {repo.get('as_of','')}).",
            "conclusion": "Policy setting — the anchor under every other rate.",
            "rule": "RBI MPC repo rate (registry-maintained)",
            "source": {"name": repo.get("source", "RBI MPC"),
                       "url": "https://www.rbi.org.in/Scripts/BS_ViewMonetaryPolicy.aspx"},
        })
    return out


# ─────────────────────────────────────────────────────────────
# Derived: overall regime + ranked strongest (feed the whole site)
# ─────────────────────────────────────────────────────────────
def overall_regime(cross, per_asset):
    """A single deterministic market-regime read, from the signals only.
    Drives the Playbook lean and the Hot framing — no editorializing."""
    score = 0.0
    drivers = []
    for c in cross:
        w = 2.0 if c["id"] == "equity_regime" else 1.0
        if c["direction"] == "bullish":
            score += w
        elif c["direction"] == "bearish":
            score -= w
        if c["direction"] in ("bullish", "bearish"):
            drivers.append({"label": c["label"], "conclusion": c["conclusion"],
                            "direction": c["direction"], "source": c.get("source")})
    for v in per_asset.values():
        t = next((s for s in v["signals"] if s["id"] == "trend"), None)
        if t and t["direction"] == "bullish":
            score += 0.5
        elif t and t["direction"] == "bearish":
            score -= 0.5

    if score >= 2:
        label, stance = "Risk-on", "Signals lean toward equities and cyclicals; a growth tilt is supported."
    elif score <= -2:
        label, stance = "Risk-off", "Signals favour defensives, duration and a larger cash buffer."
    else:
        label, stance = "Mixed", "Signals are split — stay balanced and let them confirm before tilting."
    return {
        "label": label, "stance": stance, "score": round(score, 1),
        "reading": f"Signal balance {'+' if score >= 0 else ''}{score:.1f} across "
                   f"{len(cross)} cross-asset reads and {len(per_asset)} asset trends.",
        "drivers": drivers[:4],
        "rule": "weighted vote of trend & regime signals (equity regime weighted 2x)",
    }


def rank_strongest(per_asset, cross):
    """The strongest directional signals right now — what's actually working,
    ranked by earned strength. Replaces invented 'hot' conviction."""
    pool = []
    for c in cross:
        if c["direction"] in ("bullish", "bearish"):
            pool.append({"scope": "regime", "asset": c["label"], "label": c["label"],
                         "conclusion": c["conclusion"], "reading": c["reading"],
                         "direction": c["direction"], "strength": c["strength"],
                         "rule": c.get("rule"), "source": c.get("source")})
    for v in per_asset.values():
        for s in v["signals"]:
            if s["direction"] in ("bullish", "bearish") and s.get("strength", 0) >= 60:
                pool.append({"scope": "asset", "asset": v["name"], "label": s["label"],
                             "conclusion": s["conclusion"], "reading": s["reading"],
                             "direction": s["direction"], "strength": s["strength"],
                             "rule": s.get("rule"), "source": v.get("source")})
    pool.sort(key=lambda x: -x["strength"])
    return pool[:6]


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def now_ist():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")


def main():
    try:
        with open(OUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        log(f"No {OUT_FILE} — run fetch_data.py first. Nothing to do.")
        return 0

    idx = ((data.get("blocks") or {}).get("indices") or {}).get("data") or {}
    fx = ((data.get("blocks") or {}).get("fx") or {}).get("data") or {}
    rates = ((data.get("blocks") or {}).get("rates") or {}).get("data") or {}
    macro = ((data.get("blocks") or {}).get("macro") or {}).get("data") or {}

    per_asset = {}
    for key in ("nifty50", "sensex", "gold_usd", "brent_oil", "sp500"):
        a = idx.get(key)
        if not a:
            continue
        closes = a.get("daily_tail") or []
        sigs = asset_signals(key, closes)
        if sigs:
            per_asset[key] = {
                "name": NAME.get(key, key),
                "source": SOURCE.get(key),
                "last": a.get("last"),
                "as_of": a.get("last_date"),
                "signals": sigs,
            }

    cross = cross_asset_signals(idx, fx)
    cross += rates_signals(rates, macro)
    headline = build_headline(per_asset, cross)
    regime = overall_regime(cross, per_asset)
    strongest = rank_strongest(per_asset, cross)

    n_asset = sum(len(v["signals"]) for v in per_asset.values())
    coverage = "live" if per_asset else "no_data"

    data.setdefault("blocks", {})["signals"] = {
        "status": coverage,
        "generated_at": now_ist(),
        "method": "Deterministic rules on end-of-day price data. Every signal states its rule; "
                  "figures trace to the linked source.",
        "headline": headline,
        "regime": regime,
        "strongest": strongest,
        "cross_asset": cross,
        "per_asset": per_asset,
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    log("─" * 46)
    log(f"Assets read: {len(per_asset)} · asset-signals: {n_asset} · cross-asset: {len(cross)}")
    if headline:
        log(f"Headline signal: {headline['label']} — {headline['conclusion']}")
    log(f"WROTE {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
