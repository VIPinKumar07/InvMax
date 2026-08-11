#!/usr/bin/env python3
"""
InvMax narrative synthesizer  (v1 — Stage 2)
============================================
Turns the day's numbers into the site's WRITTEN VIEW: the regime read, the Hot
section notes, and the research desk note. Runs straight after fetch_data.py.

Why this exists
---------------
Before this, the regime-read paragraph was prose written once by hand, sitting
next to numbers that moved daily. That's the gap this closes: the words now
refresh on the same cycle as the data they describe.

The rules it enforces (agreed in the design session)
----------------------------------------------------
  VOICE       Readable sentence flow, precise figures (dates, %, ₹ — never
              "markets rallied"), and it lands on a conclusion rather than
              just describing. Conclusion goes LAST.
  GROUNDING   Every claim traces to a real figure in today's data. No number,
              no sentence.
  CONTINUITY  The model sees yesterday's text and the rolling figure log. It
              UPDATES the view; it does not restate it from scratch.
  SILENCE     If nothing material moved, the text is carried forward UNCHANGED
              and only the "as of" date moves. No "nothing changed today"
              narration — that is itself noise.
  LENGTH      Flexes down on quiet days. Firm upper cap so it never sprawls.

Failure handling (never silent, never blank)
--------------------------------------------
  1. Primary model call.
  2. One retry.
  3. Template fallback built straight from the numbers — plainly labelled as
     the fallback, never passed off as the real read.
  Status is always stamped: live | carried | fallback | stale, and the page
  shows a matching badge.

Env:
  GROQ_API_KEY        primary — free tier at groq.com (Llama 3.3 70B)
  GITHUB_TOKEN        fallback — auto-available in GitHub Actions (GitHub Models)
  INVMAX_MODEL        optional, overrides the default model for the primary provider
"""

import json
import os
import sys
import re
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = os.environ.get("INVMAX_OUT", "data")
OUT_FILE = os.path.join(OUT_DIR, "latest.json")
HIST_FILE = os.path.join(OUT_DIR, "history.json")
MAX_TOKENS = 1600

log = lambda *a: print("[narrative]", *a, flush=True)


def now_ist():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ─────────────────────────────────────────────────────────────
# The prompt — this is where the voice actually lives
# ─────────────────────────────────────────────────────────────
SYSTEM = """You write the daily market view for InvMax, an India-focused research site read by ONE long-term investor. Not a trader. He checks in every day or two and wants to know what, if anything, deserves his attention.

HOW YOU WRITE
- Readable sentences that flow — not clipped terminal-speak, not padded essay prose.
- Precise figures always: actual levels, percentages, rupee/dollar amounts, dates. Never "markets rallied", "sentiment improved", "showed strength". If you cannot point to a number in the supplied data for a sentence, do not write that sentence.
- Land on a conclusion. Describe, then say what it means. The conclusion goes LAST, not first.
- No jargon without its plain meaning. No hedging filler ("it remains to be seen", "time will tell", "investors should monitor").
- Never give personalised advice or tell him to buy/sell. You frame what the evidence suggests and what would change the picture.

CONTINUITY — THIS IS THE MOST IMPORTANT RULE
You are continuing a running commentary, not writing fresh each day. You are given yesterday's text and a log of recent figures.
- If something MATERIAL changed: say what shifted and what it means for the view you held. Reference the prior view where it genuinely helps him update his thinking.
- If nothing material changed: return the carry_forward flag as true and leave the text fields empty. Do NOT write "nothing much changed today" or restate yesterday in new words. Silence is the correct output on a quiet day.
- Never narrate immaterial noise. A 0.3% index move is not news. A macro print, a flow reversal, a trend break IS.

GROUNDING — THE MOST IMPORTANT RULE
You are given a block of OBSERVED SIGNALS computed deterministically from real price data. These are your facts.
- Build your read FROM those signals. Your conclusion must match what they say — if the signals are bullish, you are not bearish.
- Every number you write must already appear in the data you were given. Never introduce a figure the data doesn't contain.
- Write a short, conclusive HEADLINE (<= 12 words) that captures the single most important observed signal — an intuitive hook, not a hedge.
- Be conclusive, not wishy-washy. The reader wants a clear read with its reasoning, then a link to dig deeper. State the view, name the signal it rests on, and note what would flip it.

LENGTH
- headline: <= 12 words, conclusive.
- regime_read: 2-5 sentences. Fewer on quieter days. Never more than 5.
- Each hot_note: one sentence, max 30 words.
- research_note: 2-3 sentences.

OUTPUT
Return ONLY valid JSON, no markdown fence, no preamble:
{
  "carry_forward": false,
  "headline": "...",
  "regime_read": "...",
  "hot_notes": {"conservative":"...","balanced":"...","aggressive":"...","special":"..."},
  "research_note": "...",
  "confidence": "high|medium|low"
}
If carry_forward is true, set the text fields to empty strings."""


def build_user_prompt(latest, history, prev_narr):
    kf = latest.get("key_figures", {})
    ch = latest.get("changes", {})
    blocks = latest.get("blocks", {})

    lines = ["TODAY'S DATA", "=" * 40]

    for key in ("nifty50", "sensex", "sp500", "gold_usd", "brent_oil"):
        v = kf.get(key)
        if isinstance(v, dict) and v.get("last") is not None:
            chg = f" ({v['chg_pct']:+.2f}% on the day)" if v.get("chg_pct") is not None else ""
            lines.append(f"{key}: {v['last']:,.2f}{chg}  [as of {v.get('date')}]")

    if isinstance(kf.get("usdinr"), dict):
        lines.append(f"USD/INR: {kf['usdinr'].get('last')}  [as of {kf['usdinr'].get('date')}]")

    for key in ("cpi_inflation_yoy", "gdp_growth", "fx_reserves_usd"):
        v = kf.get(key)
        if isinstance(v, dict) and v.get("value") is not None:
            lines.append(f"{key}: {v['value']} (period {v.get('period')})")

    for key in ("fii_net", "dii_net"):
        v = kf.get(key)
        if isinstance(v, dict):
            lines.append(f"{key}: Rs {v.get('net'):,.0f} Cr [{v.get('date')}]")

    news = (blocks.get("news") or {}).get("data") or []
    if news:
        lines.append("")
        lines.append("HEADLINES TODAY (source in brackets)")
        for n in news[:14]:
            lines.append(f"- {n.get('title')} [{n.get('source')}]")

    lines.append("")
    lines.append("WHAT THE CHANGE DETECTOR FLAGGED AS MATERIAL")
    mat = ch.get("material") or []
    if mat:
        for m in mat:
            lines.append(f"- {m.get('label')}: {m.get('detail')}")
    else:
        lines.append("- NOTHING material. Thresholds not crossed.")
    if ch.get("since"):
        lines.append(f"(compared against {ch['since']})")

    # ── OBSERVED SIGNALS: the deterministic ground truth the narration must rest on ──
    sig = (blocks.get("signals") or {})
    hl = sig.get("headline")
    cross = sig.get("cross_asset") or []
    if hl or cross:
        lines.append("")
        lines.append("OBSERVED SIGNALS (computed from real price data — these are your FACTS)")
        if hl:
            lines.append(f"- HEADLINE · {hl.get('label')}: {hl.get('conclusion')} ({hl.get('reading')})")
        for c in cross:
            lines.append(f"- {c.get('label')} [{c.get('direction')}]: {c.get('conclusion')} — {c.get('reading')}")
        # a couple of the strongest per-asset reads for texture
        pa = sig.get("per_asset") or {}
        for k, v in list(pa.items())[:2]:
            strong = [s for s in v.get("signals", []) if s.get("strength", 0) >= 65]
            for s in strong[:2]:
                lines.append(f"- {v.get('name')} {s.get('label')}: {s.get('conclusion')} ({s.get('reading')})")
        lines.append("Rule: build your read FROM these signals. Do not contradict them, and do not "
                     "introduce a market direction they don't support.")

    if history and len(history) > 1:
        lines.append("")
        lines.append("RECENT FIGURE LOG (oldest first, for trend context)")
        for snap in history[-6:]:
            bits = [f"{snap.get('date')}"]
            for key in ("nifty50", "usdinr"):
                v = snap.get(key)
                if isinstance(v, dict) and v.get("last") is not None:
                    bits.append(f"{key}={v['last']:,.2f}")
            lines.append("  " + " | ".join(bits))

    lines.append("")
    lines.append("YOUR PREVIOUS VIEW (what he read last time)")
    if prev_narr and prev_narr.get("regime_read"):
        lines.append(f"regime_read: {prev_narr['regime_read']}")
        for k, v in (prev_narr.get("hot_notes") or {}).items():
            lines.append(f"hot_notes.{k}: {v}")
        if prev_narr.get("research_note"):
            lines.append(f"research_note: {prev_narr['research_note']}")
        lines.append(f"(written {prev_narr.get('generated_at')})")
    else:
        lines.append("None — this is the first run. Write the view fresh, and do NOT "
                     "set carry_forward.")

    lines.append("")
    lines.append("Write today's view. Remember: if nothing material moved, set "
                 "carry_forward true and leave the text empty.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Model call
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Model call — cascading: Groq first, GitHub Models fallback
# Both use OpenAI-compatible chat completions, so one function
# handles both; only the URL, key and model name change.
# ─────────────────────────────────────────────────────────────
PROVIDERS = [
    {
        "name": "Groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model_env": "INVMAX_MODEL",
        "model_default": "llama-3.3-70b-versatile",
    },
    {
        "name": "GitHub Models",
        "url": "https://models.inference.ai.azure.com/chat/completions",
        "key_env": "GITHUB_TOKEN",
        "model_env": None,
        "model_default": "Meta-Llama-3.1-70B-Instruct",
    },
]


def call_openai_compat(provider, system, user):
    """Call an OpenAI-compatible chat endpoint. Returns parsed dict or None."""
    key = os.environ.get(provider["key_env"])
    if not key:
        log(f"  {provider['name']}: no {provider['key_env']} — skipping")
        return None, None
    try:
        import requests
    except ImportError:
        log("requests not installed")
        return None, None

    model = (os.environ.get(provider.get("model_env") or "") or
             provider["model_default"])

    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    try:
        r = requests.post(
            provider["url"],
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=90,
        )
        if r.status_code != 200:
            log(f"  ! {provider['name']} HTTP {r.status_code}: {r.text[:200]}")
            return None, model
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        parsed = json.loads(text)
        log(f"  {provider['name']} ({model}) responded, "
            f"carry_forward={parsed.get('carry_forward')}")
        return parsed, model
    except json.JSONDecodeError:
        log(f"  ! {provider['name']} returned non-JSON")
        return None, model
    except Exception as e:
        log(f"  ! {provider['name']} error: {type(e).__name__}")
        return None, model


def call_model(system, user, _attempt=1):
    """Try each provider in order. First success wins."""
    for prov in PROVIDERS:
        log(f"Trying {prov['name']}…")
        result, model = call_openai_compat(prov, system, user)
        if result is not None:
            return result, model or prov["model_default"]
    return None, None


# ─────────────────────────────────────────────────────────────
# Template fallback — plain, honest, built only from numbers
# ─────────────────────────────────────────────────────────────
def template_fallback(latest):
    kf = latest.get("key_figures", {})
    mat = (latest.get("changes") or {}).get("material") or []
    parts = []

    n = kf.get("nifty50")
    if isinstance(n, dict) and n.get("last"):
        chg = f", {n['chg_pct']:+.2f}% on the day" if n.get("chg_pct") is not None else ""
        parts.append(f"Nifty 50 at {n['last']:,.0f}{chg} (as of {n.get('date')}).")

    fx = kf.get("usdinr")
    if isinstance(fx, dict) and fx.get("last"):
        parts.append(f"USD/INR at {fx['last']:.2f}.")

    cpi = kf.get("cpi_inflation_yoy")
    if isinstance(cpi, dict) and cpi.get("value") is not None:
        parts.append(f"Latest CPI print {cpi['value']}% ({cpi.get('period')}).")

    if mat:
        parts.append("Flagged as material: " +
                     "; ".join(f"{m.get('label')} {m.get('detail')}" for m in mat[:3]) + ".")

    parts.append("This is an automatic summary of the figures — the written view "
                 "could not be generated this run.")

    txt = " ".join(parts)
    return {
        "carry_forward": False,
        "regime_read": txt,
        "hot_notes": {k: "Written view unavailable this run — figures above are current."
                      for k in ("conservative", "balanced", "aggressive", "special")},
        "research_note": "Written view unavailable this run. The underlying data is current.",
        "confidence": "low",
    }


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    latest = load_json(OUT_FILE, None)
    if not latest:
        log(f"No {OUT_FILE} — run fetch_data.py first. Nothing to do.")
        return 0

    history = load_json(HIST_FILE, [])
    prev_narr = ((latest.get("blocks") or {}).get("narrative") or {})
    prev_has_text = bool(prev_narr.get("regime_read"))

    material = (latest.get("changes") or {}).get("material") or []
    log(f"Material changes today: {len(material)}")

    user = build_user_prompt(latest, history, prev_narr if prev_has_text else None)

    result, model_used = call_model(SYSTEM, user)

    if result is None:
        if prev_has_text:
            # keep yesterday's real words rather than downgrade to a template
            log("Generation failed — keeping previous view, marked stale")
            narrative = dict(prev_narr)
            narrative["status"] = "stale"
            narrative["stale_since"] = prev_narr.get("generated_at")
            narrative["checked_at"] = now_ist()
        else:
            log("Generation failed and no previous view — using template fallback")
            narrative = template_fallback(latest)
            narrative["status"] = "fallback"
            narrative["generated_at"] = now_ist()
            narrative["model"] = "template"
    elif result.get("carry_forward") and prev_has_text:
        # the quiet-day path: text does not move, only the date does
        log("Nothing material — carrying previous view forward unchanged")
        narrative = dict(prev_narr)
        narrative["status"] = "carried"
        narrative["checked_at"] = now_ist()
        narrative["unchanged_since"] = prev_narr.get("generated_at")
    else:
        narrative = {
            "status": "live",
            "generated_at": now_ist(),
            "model": model_used or "unknown",
            "headline": (result.get("headline") or "").strip(),
            "regime_read": (result.get("regime_read") or "").strip(),
            "hot_notes": result.get("hot_notes") or {},
            "research_note": (result.get("research_note") or "").strip(),
            "confidence": result.get("confidence", "medium"),
        }
        if not narrative["regime_read"]:
            if prev_has_text:
                narrative = dict(prev_narr)
                narrative["status"] = "carried"
                narrative["checked_at"] = now_ist()
            else:
                narrative = template_fallback(latest)
                narrative["status"] = "fallback"
                narrative["generated_at"] = now_ist()
                narrative["model"] = "template"

    latest.setdefault("blocks", {})["narrative"] = narrative
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(latest, f, indent=2, ensure_ascii=False)

    log("─" * 40)
    log(f"Status: {narrative.get('status')}")
    preview = (narrative.get("regime_read") or "")[:130]
    log(f"Regime read: {preview}{'…' if len(preview) == 130 else ''}")
    log(f"WROTE {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
