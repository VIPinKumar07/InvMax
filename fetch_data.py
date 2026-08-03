#!/usr/bin/env python3
"""
InvMax daily data fetcher  (v2 — Stages 1 & 4)
==============================================
Pulls free public sources listed in `sources.json` and writes `data/latest.json`.

What's new in v2:
  * SOURCES ARE DATA, NOT CODE. Everything comes from `sources.json`, so the
    Source Manager on the home page can add/remove feeds without touching Python.
  * ROLLING HISTORY (`data/history.json`) — the last 30 days of key figures.
    This is what gives the narrative step continuity ("what did I say yesterday").
  * CHANGE DETECTION with MATERIALITY THRESHOLDS. Every run diffs against the
    previous run and classifies each change as material or not. This drives both
    the "What changed" strip on the home page and the narrative step's instinct
    to stay quiet when nothing important moved.

Design rules (unchanged):
  * Every source is wrapped — one failure NEVER breaks the run.
  * Last-good values are preserved if a source is down.
  * Every block is stamped with `asof`.
  * Always exits 0 so CI never goes red over a flaky feed.

Run locally:   python fetch_data.py
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
HIST_FILE = os.path.join(OUT_DIR, "history.json")
TIMEOUT = 25
HISTORY_KEEP = 30

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/csv, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

log = lambda *a: print("[invmax]", *a, flush=True)


# ─────────────────────────────────────────────────────────────
# Source registry
# ─────────────────────────────────────────────────────────────
DEFAULT_SOURCES = {
    "news": [
        {"id": "business-standard", "name": "Business Standard",
         "url": "https://www.business-standard.com/rss/markets-106.rss", "enabled": True},
        {"id": "finshots-daily", "name": "Finshots Daily",
         "url": "https://finshots.in/rss/", "enabled": True},
        {"id": "the-core", "name": "The Core",
         "url": "https://www.thecore.in/rss.xml", "enabled": True},
        {"id": "economic-times", "name": "Economic Times",
         "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "enabled": True},
        {"id": "mint", "name": "Mint",
         "url": "https://www.livemint.com/rss/markets", "enabled": True},
        {"id": "moneycontrol", "name": "Moneycontrol",
         "url": "https://www.moneycontrol.com/rss/marketreports.xml", "enabled": True},
    ],
    "indices": [
        {"id": "nifty50", "name": "Nifty 50", "symbol": "^nsei", "enabled": True},
        {"id": "sensex", "name": "Sensex", "symbol": "^bse", "enabled": True},
        {"id": "sp500", "name": "S&P 500", "symbol": "^spx", "enabled": True},
        {"id": "gold_usd", "name": "Gold (USD)", "symbol": "xauusd", "enabled": True},
    ],
    "funds": [
        {"id": "119551", "name": "Flexicap (sample)", "enabled": True},
        {"id": "120503", "name": "Large Cap (sample)", "enabled": True},
        {"id": "118989", "name": "Mid Cap (sample)", "enabled": True},
        {"id": "119063", "name": "Debt / Short Duration (sample)", "enabled": True},
    ],
    "macro": [
        {"id": "cpi_inflation_yoy", "name": "CPI inflation (YoY)",
         "series": "WB/WDI/FP.CPI.TOTL.ZG-IN", "enabled": True},
        {"id": "gdp_growth", "name": "GDP growth",
         "series": "WB/WDI/NY.GDP.MKTP.KD.ZG-IN", "enabled": True},
        {"id": "fx_reserves_usd", "name": "FX reserves (USD)",
         "series": "WB/WDI/FI.RES.TOTL.CD-IN", "enabled": True},
    ],
    "flows": [
        {"id": "nse_fii_dii", "name": "NSE FII/DII cash-market flows",
         "url": "https://www.nseindia.com/api/fiidiiTradeReact", "enabled": True},
    ],
}


def load_sources():
    """Read sources.json; fall back to built-in defaults if absent/broken."""
    for path in ("sources.json", os.path.join(OUT_DIR, "sources.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = json.load(f)
            merged = dict(DEFAULT_SOURCES)
            for k in DEFAULT_SOURCES:
                if isinstance(s.get(k), list) and s[k]:
                    merged[k] = s[k]
            log(f"Sources: loaded {path}")
            return merged
        except FileNotFoundError:
            continue
        except Exception as e:
            log(f"  ! sources.json unreadable ({type(e).__name__}) — using defaults")
            break
    log("Sources: using built-in defaults")
    return dict(DEFAULT_SOURCES)


def enabled(items):
    return [x for x in (items or []) if x.get("enabled", True)]


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


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ─────────────────────────────────────────────────────────────
# 1. FX
# ─────────────────────────────────────────────────────────────
def fetch_fx(_src):
    out = {"source": None, "asof": None, "rates": {}, "usdinr_history": []}
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

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=365)
    r = get(f"https://api.frankfurter.dev/v1/{start}..{end}?base=USD&symbols=INR")
    if r:
        try:
            d = r.json().get("rates", {})
            out["usdinr_history"] = [{"date": k, "v": round(v["INR"], 4)} for k, v in sorted(d.items())]
        except Exception:
            pass

    log(f"FX: {out['source']} | USD/INR={out['rates'].get('INR')} | "
        f"{len(out['usdinr_history'])} history points")
    return out if out["rates"] else None


# ─────────────────────────────────────────────────────────────
# 2. MUTUAL FUND NAVs
# ─────────────────────────────────────────────────────────────
def fetch_funds(src):
    funds = []
    for item in enabled(src.get("funds")):
        code, label = str(item.get("id")), item.get("name", "")
        r = get(f"https://api.mfapi.in/mf/{code}")
        if not r:
            continue
        try:
            d = r.json()
            data = d.get("data") or []
            if not data:
                continue
            latest = data[0]
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
                "series": [round(float(x["nav"]), 4) for x in list(reversed(hist))[::21]],
            })
        except Exception as e:
            log(f"  ! fund {code}: {type(e).__name__}")
    log(f"Funds: {len(funds)} schemes")
    return funds or None


# ─────────────────────────────────────────────────────────────
# 3. MACRO
# ─────────────────────────────────────────────────────────────
def fetch_macro(src):
    out = {}
    for item in enabled(src.get("macro")):
        label, series = item.get("id"), item.get("series")
        if not series:
            continue
        r = get(f"https://api.db.nomics.world/v22/series/{series}?observations=1")
        if not r:
            continue
        try:
            docs = r.json()["series"]["docs"][0]
            pairs = [(p, v) for p, v in zip(docs.get("period", []), docs.get("value", []))
                     if v is not None and not isinstance(v, str)]
            if pairs:
                p, v = pairs[-1]
                out[label] = {"value": round(float(v), 2), "period": p,
                              "name": item.get("name", label),
                              "source": "World Bank via DBnomics"}
        except Exception as e:
            log(f"  ! macro {label}: {type(e).__name__}")
    log(f"Macro: {len(out)} series")
    return out or None


# ─────────────────────────────────────────────────────────────
# 4. INDEX HISTORY
# ─────────────────────────────────────────────────────────────
def fetch_index(symbol):
    r = get(f"https://stooq.com/q/d/l/?s={quote(symbol)}&i=d")
    if not r or "Date" not in r.text[:200]:
        return None
    try:
        rows = [x for x in csv.DictReader(io.StringIO(r.text))
                if x.get("Close") not in (None, "", "N/D")]
        if len(rows) < 30:
            return None
        rows = rows[-1300:]
        closes = [{"date": x["Date"], "c": float(x["Close"])} for x in rows]
        by_month = {}
        for p in closes:
            by_month[p["date"][:7]] = p
        month_series = [by_month[k] for k in sorted(by_month)]
        prev = closes[-2]["c"] if len(closes) > 1 else closes[-1]["c"]
        return {
            "last": closes[-1]["c"],
            "last_date": closes[-1]["date"],
            "prev_close": prev,
            "chg_pct": round((closes[-1]["c"] / prev - 1) * 100, 2) if prev else 0,
            "daily_tail": closes[-60:],
            "monthly": month_series[-61:],
        }
    except Exception as e:
        log(f"  ! index {symbol}: {type(e).__name__}")
        return None


def fetch_indices(src):
    out = {}
    for item in enabled(src.get("indices")):
        d = fetch_index(item.get("symbol", ""))
        if d:
            d["name"] = item.get("name", item.get("id"))
            out[item["id"]] = d
            log(f"  {item['id']}: {d['last']} on {d['last_date']} ({len(d['monthly'])} months)")
    log(f"Indices: {len(out)} series")
    return out or None


# ─────────────────────────────────────────────────────────────
# 5. FII / DII FLOWS  (official NSE; commonly blocks bots — degrade gracefully)
# ─────────────────────────────────────────────────────────────
def fetch_flows(src):
    for item in enabled(src.get("flows")):
        url = item.get("url")
        if not url:
            continue
        try:
            s = requests.Session()
            s.headers.update(dict(UA, Referer="https://www.nseindia.com/"))
            s.get("https://www.nseindia.com/", timeout=TIMEOUT)   # cookie handshake
            r = s.get(url, timeout=TIMEOUT)
            if r.status_code != 200:
                log(f"  ! flows -> HTTP {r.status_code}")
                continue
            rows = r.json()
            out = {}
            for row in rows:
                cat = str(row.get("category", "")).upper()
                key = "fii" if "FII" in cat or "FPI" in cat else ("dii" if "DII" in cat else None)
                if not key:
                    continue
                out[key] = {
                    "date": row.get("date"),
                    "buy": float(row.get("buyValue") or 0),
                    "sell": float(row.get("sellValue") or 0),
                    "net": float(row.get("netValue") or 0),
                }
            if out:
                log(f"Flows: FII net={out.get('fii', {}).get('net')} "
                    f"DII net={out.get('dii', {}).get('net')}")
                return out
        except Exception as e:
            log(f"  ! flows: {type(e).__name__}")
    log("Flows: unavailable (NSE commonly blocks automated access)")
    return None


# ─────────────────────────────────────────────────────────────
# 6. NEWS
# ─────────────────────────────────────────────────────────────
def strip_html(s):
    s = s or ""
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
          .replace("&apos;", "'").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&nbsp;", " ").replace("&rsquo;", "'").replace("&ldquo;", '"')
          .replace("&rdquo;", '"'))
    return re.sub(r"\s+", " ", s).strip()


def fetch_news(src, limit_per_feed=6):
    items, seen = [], set()
    feeds = enabled(src.get("news"))
    for feed in feeds:
        url, source = feed.get("url"), feed.get("name", feed.get("id", "?"))
        if not url:
            continue
        r = get(url, retries=1)
        if not r:
            continue
        try:
            blocks = re.findall(r"<item[^>]*>(.*?)</item>", r.text, re.S | re.I)
            if not blocks:  # Atom fallback (some Ghost blogs / The Core)
                blocks = re.findall(r"<entry[^>]*>(.*?)</entry>", r.text, re.S | re.I)
            for b in blocks[:limit_per_feed]:
                title = re.search(r"<title[^>]*>(.*?)</title>", b, re.S | re.I)
                link = re.search(r"<link[^>]*>(.*?)</link>", b, re.S | re.I)
                if not link:
                    link = re.search(r'<link[^>]*href=["\'](.*?)["\']', b, re.S | re.I)
                date = re.search(r"<(?:pubDate|published|updated)[^>]*>(.*?)</(?:pubDate|published|updated)>",
                                 b, re.S | re.I)
                if not title:
                    continue
                t = strip_html(title.group(1))
                key = t.lower()[:70]
                if not t or key in seen:
                    continue
                seen.add(key)
                items.append({
                    "source": source,
                    "weight": feed.get("weight", "medium"),
                    "title": t[:180],
                    "url": strip_html(link.group(1)) if link else "",
                    "published": strip_html(date.group(1)) if date else "",
                })
        except Exception as e:
            log(f"  ! rss {source}: {type(e).__name__}")
    log(f"News: {len(items)} headlines from {len(feeds)} feeds")
    return items or None


# ─────────────────────────────────────────────────────────────
# KEY FIGURES + CHANGE DETECTION  (Stage 4)
# ─────────────────────────────────────────────────────────────
THRESHOLDS = {"index_pct": 1.0, "fx_pct": 0.5, "gold_pct": 1.0, "flow_cr": 2000.0}

LABELS = {
    "nifty50": "Nifty 50", "sensex": "Sensex", "sp500": "S&P 500",
    "gold_usd": "Gold", "usdinr": "USD/INR",
    "cpi_inflation_yoy": "CPI inflation", "gdp_growth": "GDP growth",
    "fx_reserves_usd": "FX reserves", "fii_net": "FII flows", "dii_net": "DII flows",
}


def nice(key):
    return LABELS.get(key, key.replace("_", " ").title())


def key_figures(blocks):
    """A compact snapshot used for history, diffing and the narrative prompt."""
    kf = {"date": now_ist()}
    idx = (blocks.get("indices") or {}).get("data") or {}
    for k, v in idx.items():
        kf[k] = {"last": v.get("last"), "date": v.get("last_date"), "chg_pct": v.get("chg_pct")}
    fx = (blocks.get("fx") or {}).get("data") or {}
    if fx.get("rates", {}).get("INR"):
        kf["usdinr"] = {"last": round(fx["rates"]["INR"], 4), "date": fx.get("asof")}
    macro = (blocks.get("macro") or {}).get("data") or {}
    for k, v in macro.items():
        kf[k] = {"value": v.get("value"), "period": v.get("period")}
    flows = (blocks.get("flows") or {}).get("data") or {}
    for k, v in flows.items():
        kf[k + "_net"] = {"net": v.get("net"), "date": v.get("date")}
    news = (blocks.get("news") or {}).get("data") or []
    kf["headlines"] = [n.get("title") for n in news[:12]]
    return kf


def pct(new, old):
    try:
        if old:
            return (float(new) / float(old) - 1) * 100
    except Exception:
        pass
    return None


def detect_changes(now_kf, prev_kf):
    """Classify what moved. `material` drives the UI strip and the narrative's
    decision to speak up; everything else is recorded but stays quiet."""
    out = {"material": [], "all": [], "since": (prev_kf or {}).get("date")}
    if not prev_kf:
        out["material"].append({"kind": "init", "label": "First data run",
                                "detail": "Baseline captured; changes tracked from tomorrow."})
        return out

    def add(kind, label, detail, material):
        rec = {"kind": kind, "label": label, "detail": detail}
        out["all"].append(rec)
        if material:
            out["material"].append(rec)

    for k, v in now_kf.items():
        p = prev_kf.get(k)
        if not isinstance(v, dict) or not isinstance(p, dict):
            continue

        if "last" in v and "last" in p and v["last"] is not None and p["last"] is not None:
            ch = pct(v["last"], p["last"])
            if ch is None:
                continue
            if k == "usdinr":
                add("fx", nice(k), f"₹{v['last']:.2f} ({ch:+.2f}%)",
                    abs(ch) >= THRESHOLDS["fx_pct"])
            elif k == "gold_usd":
                add("gold", nice(k), f"${v['last']:,.0f} ({ch:+.2f}%)",
                    abs(ch) >= THRESHOLDS["gold_pct"])
            else:
                add("index", nice(k), f"{v['last']:,.0f} ({ch:+.2f}%)",
                    abs(ch) >= THRESHOLDS["index_pct"])

        elif "value" in v and "value" in p:
            if v.get("period") != p.get("period") or v.get("value") != p.get("value"):
                add("macro", nice(k), f"{v['value']} ({v.get('period')})", True)

        elif "net" in v and "net" in p:
            flip = (v["net"] or 0) * (p["net"] or 0) < 0
            big = abs((v["net"] or 0) - (p["net"] or 0)) >= THRESHOLDS["flow_cr"]
            add("flow", nice(k),
                f"₹{v['net']:,.0f} Cr" + (" — direction flipped" if flip else ""),
                flip or big)

    old_titles = set(prev_kf.get("headlines") or [])
    fresh = [t for t in (now_kf.get("headlines") or []) if t not in old_titles]
    if fresh:
        add("news", "New headlines", f"{len(fresh)} new", True)
        out["new_headlines"] = fresh[:8]

    return out


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    src = load_sources()
    prev = load_json(OUT_FILE, {})
    history = load_json(HIST_FILE, [])

    out = {
        "generated_at": now_ist(),
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Daily refresh. Free public sources. Delayed EOD data — not real-time.",
        "sources_used": {k: [i.get("name") for i in enabled(src.get(k))]
                         for k in ("news", "indices", "funds", "macro", "flows")},
        "blocks": {},
    }

    tasks = [("fx", fetch_fx), ("funds", fetch_funds), ("macro", fetch_macro),
             ("indices", fetch_indices), ("flows", fetch_flows), ("news", fetch_news)]

    ok, failed = [], []
    for name, fn in tasks:
        log(f"Fetching {name} …")
        try:
            data = fn(src)
        except Exception as e:
            log(f"  !! {name} crashed: {type(e).__name__}: {e}")
            data = None

        if data:
            out["blocks"][name] = {"asof": now_ist(), "status": "ok", "data": data}
            ok.append(name)
        else:
            old = (prev.get("blocks") or {}).get(name)
            if old:
                old = dict(old)
                old["status"] = "stale"
                out["blocks"][name] = old
                log("  -> kept previous value (stale)")
            else:
                out["blocks"][name] = {"asof": now_ist(), "status": "unavailable", "data": None}
            failed.append(name)

    # ---- key figures, history, change detection -------------------------
    kf = key_figures(out["blocks"])
    prev_kf = history[-1] if history else None
    out["key_figures"] = kf
    out["changes"] = detect_changes(kf, prev_kf)

    history.append(kf)
    history = history[-HISTORY_KEEP:]
    with open(HIST_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=1, ensure_ascii=False)

    # carry forward any existing narrative; synthesize.py overwrites it next
    if (prev.get("blocks") or {}).get("narrative"):
        out["blocks"]["narrative"] = prev["blocks"]["narrative"]

    out["summary"] = {"ok": ok, "failed": failed, "healthy": len(ok), "total": len(tasks)}

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    log("─" * 46)
    log(f"WROTE {OUT_FILE}")
    log(f"OK: {', '.join(ok) if ok else 'none'}")
    if failed:
        log(f"FAILED/STALE: {', '.join(failed)}")
    log(f"Material changes: {len(out['changes']['material'])}")
    log(f"History: {len(history)} snapshots")
    log(f"Size: {os.path.getsize(OUT_FILE)/1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
