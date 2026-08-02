#!/usr/bin/env python3
"""
InvMax daily data fetcher
=========================
Pulls genuinely public, free data sources and writes a single `data/latest.json`
that the InvMax website reads.

Design rules:
  * Every source is wrapped — one failure NEVER breaks the run.
  * Last-good values are preserved if a source is down.
  * Every block is stamped with `asof` so the site can show data freshness.
  * No API keys required for the default set (keys are optional extras).

Run locally:   python fetch_data.py
Run on CI:     handled by .github/workflows/update-data.yml (daily)
"""

import json
import os
import sys
import csv
import io
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("Please: pip install requests")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = os.environ.get("INVMAX_OUT", "data")
OUT_FILE = os.path.join(OUT_DIR, "latest.json")
TIMEOUT = 25

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/csv, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

log = lambda *a: print("[invmax]", *a, flush=True)


def get(url, headers=None, params=None, retries=2):
    """GET with retries. Returns response or None."""
    h = dict(UA)
    if headers:
        h.update(headers)
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=h, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            log(f"  ! {url[:70]} -> HTTP {r.status_code}")
        except Exception as e:
            log(f"  ! {url[:70]} -> {type(e).__name__}")
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return None


def now_ist():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")


def load_previous():
    """Keep last-good data so a failed source never blanks the site."""
    try:
        with open(OUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────
# 1. FX  —  free, no key  (Frankfurter / ECB + fallbacks)
# ─────────────────────────────────────────────────────────────
def fetch_fx():
    out = {"source": None, "asof": None, "rates": {}, "usdinr_history": []}

    # current rates
    for url, parse in [
        ("https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR,EUR,GBP,JPY",
         lambda d: (d["rates"], d.get("date"), "ECB/Frankfurter")),
        ("https://open.er-api.com/v6/latest/USD",
         lambda d: ({k: d["rates"][k] for k in ("INR", "EUR", "GBP", "JPY") if k in d.get("rates", {})},
                    (d.get("time_last_update_utc") or "")[5:16], "exchangerate-api")),
    ]:
        r = get(url)
        if not r:
            continue
        try:
            rates, date, src = parse(r.json())
            if rates.get("INR"):
                out.update({"rates": rates, "asof": date, "source": src})
                break
        except Exception:
            continue

    # 1-year daily USD/INR history (for the sparkline + computed stats)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=365)
    r = get(f"https://api.frankfurter.dev/v1/{start}..{end}?base=USD&symbols=INR")
    if r:
        try:
            d = r.json().get("rates", {})
            out["usdinr_history"] = [
                {"date": k, "v": round(v["INR"], 4)} for k, v in sorted(d.items())
            ]
        except Exception:
            pass

    log(f"FX: {out['source']} | USD/INR={out['rates'].get('INR')} | "
        f"{len(out['usdinr_history'])} history points")
    return out if out["rates"] else None


# ─────────────────────────────────────────────────────────────
# 2. MUTUAL FUND NAVs  —  free, no key  (mfapi.in over AMFI)
# ─────────────────────────────────────────────────────────────
# A small representative basket. Find more codes at https://api.mfapi.in/mf
FUND_CODES = {
    "119551": "Flexicap (sample)",
    "120503": "Large Cap (sample)",
    "118989": "Mid Cap (sample)",
    "119063": "Debt / Short Duration (sample)",
}


def fetch_funds():
    funds = []
    for code, label in FUND_CODES.items():
        r = get(f"https://api.mfapi.in/mf/{code}")
        if not r:
            continue
        try:
            d = r.json()
            data = d.get("data") or []
            if not data:
                continue
            latest = data[0]
            # 1-year return from the NAV history
            hist = data[:400]
            ret1y = None
            if len(hist) > 200:
                try:
                    new = float(hist[0]["nav"])
                    old = float(hist[min(len(hist) - 1, 250)]["nav"])
                    if old:
                        ret1y = round((new / old - 1) * 100, 2)
                except Exception:
                    pass
            funds.append({
                "code": code,
                "label": label,
                "scheme": (d.get("meta") or {}).get("scheme_name", ""),
                "nav": float(latest["nav"]),
                "date": latest["date"],
                "ret1y_pct": ret1y,
                # monthly-ish series (every ~21 trading days), oldest first
                "series": [round(float(x["nav"]), 4)
                           for x in list(reversed(hist))[::21]],
            })
        except Exception as e:
            log(f"  ! fund {code}: {type(e).__name__}")
    log(f"Funds: {len(funds)} schemes")
    return funds or None


# ─────────────────────────────────────────────────────────────
# 3. MACRO  —  free, no key  (DBnomics aggregates RBI / MOSPI / WB)
# ─────────────────────────────────────────────────────────────
MACRO_SERIES = {
    # label: DBnomics series path
    "cpi_inflation_yoy":  "WB/WDI/FP.CPI.TOTL.ZG-IN",
    "gdp_growth":         "WB/WDI/NY.GDP.MKTP.KD.ZG-IN",
    "fx_reserves_usd":    "WB/WDI/FI.RES.TOTL.CD-IN",
}


def fetch_macro():
    out = {}
    for label, series in MACRO_SERIES.items():
        r = get(f"https://api.db.nomics.world/v22/series/{series}?observations=1")
        if not r:
            continue
        try:
            docs = r.json()["series"]["docs"][0]
            periods = docs.get("period", [])
            values = docs.get("value", [])
            pairs = [(p, v) for p, v in zip(periods, values)
                     if v is not None and not isinstance(v, str)]
            if pairs:
                p, v = pairs[-1]
                out[label] = {"value": round(float(v), 2), "period": p,
                              "source": "World Bank via DBnomics"}
        except Exception as e:
            log(f"  ! macro {label}: {type(e).__name__}")
    log(f"Macro: {len(out)} series")
    return out or None


# ─────────────────────────────────────────────────────────────
# 4. INDEX HISTORY  —  free, no key  (Stooq EOD CSV)
#    This is the highest-leverage feed: the analytics page COMPUTES
#    volatility, Sharpe, drawdown, correlation & the frontier from it.
# ─────────────────────────────────────────────────────────────
INDEX_SYMBOLS = {
    "nifty50": "^nsei",
    "sensex":  "^bse",
    "sp500":   "^spx",
    "gold_usd": "xauusd",
}


def fetch_index(symbol):
    r = get(f"https://stooq.com/q/d/l/?s={quote(symbol)}&i=d")
    if not r or "Date" not in r.text[:200]:
        return None
    try:
        rows = list(csv.DictReader(io.StringIO(r.text)))
        rows = [x for x in rows if x.get("Close") not in (None, "", "N/D")]
        if len(rows) < 30:
            return None
        rows = rows[-1300:]  # ~5 years of trading days
        closes = [{"date": x["Date"], "c": float(x["Close"])} for x in rows]
        # Month-end series: last observation of each calendar month.
        # This is exactly what the analytics engine needs to compute
        # returns, volatility, correlation, drawdown and the frontier.
        by_month = {}
        for p in closes:
            by_month[p["date"][:7]] = p
        month_series = [by_month[k] for k in sorted(by_month)]
        return {
            "last": closes[-1]["c"],
            "last_date": closes[-1]["date"],
            "daily_tail": closes[-60:],
            "monthly": month_series[-61:],
        }
    except Exception as e:
        log(f"  ! index {symbol}: {type(e).__name__}")
        return None


def fetch_indices():
    out = {}
    for name, sym in INDEX_SYMBOLS.items():
        d = fetch_index(sym)
        if d:
            out[name] = d
            log(f"  {name}: {d['last']} on {d['last_date']} "
                f"({len(d['monthly'])} months)")
    log(f"Indices: {len(out)} series")
    return out or None


# ─────────────────────────────────────────────────────────────
# 5. NEWS  —  free, no key  (Indian financial RSS)
# ─────────────────────────────────────────────────────────────
RSS_FEEDS = {
    "Economic Times":  "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Mint":            "https://www.livemint.com/rss/markets",
    "Business Standard": "https://www.business-standard.com/rss/markets-106.rss",
    "Moneycontrol":    "https://www.moneycontrol.com/rss/marketreports.xml",
}


def strip_html(s):
    s = s or ""
    # Unwrap CDATA first — Indian feeds use it heavily and a naive
    # tag-strip would delete the entire headline.
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
          .replace("&apos;", "'").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&nbsp;", " ").replace("&rsquo;", "'").replace("&ldquo;", '"')
          .replace("&rdquo;", '"'))
    return re.sub(r"\s+", " ", s).strip()


def fetch_news(limit_per_feed=6):
    items, seen_titles = [], set()
    for source, url in RSS_FEEDS.items():
        r = get(url, retries=1)
        if not r:
            continue
        try:
            blocks = re.findall(r"<item[^>]*>(.*?)</item>", r.text, re.S | re.I)
            for b in blocks[:limit_per_feed]:
                title = re.search(r"<title[^>]*>(.*?)</title>", b, re.S | re.I)
                link = re.search(r"<link[^>]*>(.*?)</link>", b, re.S | re.I)
                date = re.search(r"<pubDate[^>]*>(.*?)</pubDate>", b, re.S | re.I)
                if not title:
                    continue
                t = strip_html(title.group(1))
                key = t.lower()[:70]
                if not t or key in seen_titles:
                    continue
                seen_titles.add(key)
                items.append({
                    "source": source,
                    "title": t[:180],
                    "url": strip_html(link.group(1)) if link else "",
                    "published": strip_html(date.group(1)) if date else "",
                })
        except Exception as e:
            log(f"  ! rss {source}: {type(e).__name__}")
    log(f"News: {len(items)} headlines from {len(RSS_FEEDS)} feeds")
    return items or None


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    prev = load_previous()
    out = {
        "generated_at": now_ist(),
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Daily refresh. Free public sources. Delayed EOD data — not real-time.",
        "blocks": {},
    }

    tasks = [
        ("fx", fetch_fx),
        ("funds", fetch_funds),
        ("macro", fetch_macro),
        ("indices", fetch_indices),
        ("news", fetch_news),
    ]

    ok, failed = [], []
    for name, fn in tasks:
        log(f"Fetching {name} …")
        try:
            data = fn()
        except Exception as e:
            log(f"  !! {name} crashed: {type(e).__name__}: {e}")
            data = None

        if data:
            out["blocks"][name] = {"asof": now_ist(), "status": "ok", "data": data}
            ok.append(name)
        else:
            # preserve last-good so the site never goes blank
            old = (prev.get("blocks") or {}).get(name)
            if old:
                old = dict(old)
                old["status"] = "stale"
                out["blocks"][name] = old
                log(f"  -> kept previous value (stale)")
            else:
                out["blocks"][name] = {"asof": now_ist(), "status": "unavailable",
                                       "data": None}
            failed.append(name)

    out["summary"] = {"ok": ok, "failed": failed,
                      "healthy": len(ok), "total": len(tasks)}

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    log("─" * 46)
    log(f"WROTE {OUT_FILE}")
    log(f"OK: {', '.join(ok) if ok else 'none'}")
    if failed:
        log(f"FAILED/STALE: {', '.join(failed)}")
    log(f"Size: {os.path.getsize(OUT_FILE)/1024:.1f} KB")

    # Never fail the CI run just because one source was down.
    return 0


if __name__ == "__main__":
    sys.exit(main())
