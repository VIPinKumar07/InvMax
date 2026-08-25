/* InvMax shared data layer
   ─────────────────────────
   One loader used by index.html, analytics.html and research.html so every
   page reads the daily feed (data/latest.json, produced by fetch_data.py) the
   SAME way. If the file isn't there yet (e.g. opened from disk, or the daily
   robot hasn't run), every call returns null and each page falls back to its
   built-in modelled data — nothing ever breaks.

   Usage:
     const d = await InvMax.load();
     const fx = InvMax.block(d,'fx');
     if (fx) { ... use fx.data ... }
*/
window.InvMax = (function () {
  const PATH = 'data/latest.json';
  let cache; // undefined = not tried, null = failed, object = loaded

  async function load() {
    if (cache !== undefined) return cache;
    try {
      const r = await fetch(PATH, { cache: 'no-store' });
      if (!r.ok) throw 0;
      cache = await r.json();
    } catch (e) {
      cache = null;
    }
    return cache;
  }

  // return a named block only if it actually carries data
  function block(d, name) {
    const b = d && d.blocks && d.blocks[name];
    return (b && b.data) ? b : null;
  }

  // "01 Aug 2026, 07:00 IST" style stamp for the whole feed
  function asOf(d) { return d ? (d.generated_at || null) : null; }

  // convert an index monthly close series [{date,c}] → monthly returns [r...]
  function monthlyReturns(series) {
    if (!series || series.length < 3) return null;
    const out = [];
    for (let i = 1; i < series.length; i++) {
      const a = series[i - 1].c, b = series[i].c;
      if (a > 0) out.push(b / a - 1);
    }
    return out.length ? out : null;
  }

  // ── Stage 2: the written view ──────────────────────────────
  // Returns the narrative block only if it carries real text.
  function narrative(d) {
    const n = d && d.blocks && d.blocks.narrative;
    return (n && n.regime_read) ? n : null;
  }

  // How fresh is the written view? Drives the badge on every page.
  //   live     — written from today's numbers
  //   carried  — nothing material moved, yesterday's view still stands
  //   stale    — generation failed, showing the last good view
  //   fallback — plain figures-only summary, not a real read
  function narrativeBadge(n) {
    if (!n) return null;
    const map = {
      live:     { cls: 'live',     text: '● live · written ' + (n.generated_at || '') },
      carried:  { cls: 'live',     text: '● unchanged · nothing material moved' },
      stale:    { cls: 'warn',     text: '⚠ not refreshed today · showing ' + (n.stale_since || 'last view') },
      fallback: { cls: 'warn',     text: '⚠ auto-summary · written view unavailable' }
    };
    return map[n.status] || map.live;
  }

  // ── Stage 4: what changed since last look ──────────────────
  function changes(d) {
    const c = d && d.changes;
    if (!c) return null;
    const mat = c.material || [];
    return mat.length ? { list: mat, since: c.since, headlines: c.new_headlines || [] } : null;
  }

  // ── Stage 1: which sources fed this run ────────────────────
  function sourcesUsed(d) { return (d && d.sources_used) || null; }

  // ── SINGLE SOURCE OF TRUTH ─────────────────────────────────
  // One place every USD/INR readout comes from. Returns {value,date,live} or null.
  function usdinr(d) {
    const fxb = block(d, 'fx');
    const fx = fxb && fxb.data;
    const v = fx && fx.rates && fx.rates.INR;
    if (v == null) return null;
    return { value: +v, date: fx.asof || asOf(d), live: fxb && fxb.status === 'ok' };
  }

  // Generic asset accessor for the multi-asset chart & KPIs.
  // key ∈ nifty50 | sensex | gold_usd | brent_oil | (usdinr handled specially)
  function asset(d, key) {
    if (key === 'usdinr') {
      const u = usdinr(d);
      if (!u) return null;
      const fxb = block(d, 'fx');
      const hist = (fxb && fxb.data && fxb.data.usdinr_history) || [];
      return { last: u.value, date: u.date, live: u.live, name: 'USD/INR',
               series: hist.map(p => ({ t: p.date, v: p.v })) };
    }
    const idxb = block(d, 'indices');
    const a = idxb && idxb.data && idxb.data[key];
    if (!a) return null;
    const monthly = (a.monthly || []).map(p => ({ t: p.date, v: p.c }));
    const daily = (a.daily_tail || []).map(p => ({ t: p.date, v: p.c }));
    return { last: a.last, date: a.last_date, chg: a.chg_pct, live: idxb.status === 'ok',
             name: a.name || key, series: daily.length >= 20 ? daily : monthly };
  }

  // Which asset keys are actually present in the feed (for the chart legend).
  function assetsPresent(d) {
    const out = [];
    const idxb = block(d, 'indices');
    const idx = (idxb && idxb.data) || {};
    ['nifty50','sensex','gold_usd','brent_oil'].forEach(k => { if (idx[k]) out.push(k); });
    if (usdinr(d)) out.push('usdinr');
    return out;
  }

  // ── Layer 3: observed signals (deterministic, sourced) ─────
  function signals(d) {
    const s = d && d.blocks && d.blocks.signals;
    return (s && (s.headline || (s.cross_asset || []).length)) ? s : null;
  }

  // Source citations: turn feed URLs into linkable references.
  function sourceLinks(d) {
    const nb = block(d, 'news');
    const news = (nb && nb.data) || [];
    const seen = {}, links = [];
    news.forEach(n => {
      if (n.source && n.url && !seen[n.source]) {
        seen[n.source] = 1;
        try { links.push({ name: n.source, url: new URL(n.url).origin }); }
        catch (e) { /* skip malformed */ }
      }
    });
    return links.slice(0, 6);
  }

  // small helper: format an ISO-ish date to "01 Aug 26"
  function shortDate(s) {
    if (!s) return '';
    const m = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const d = new Date(s);
    if (isNaN(d)) return String(s).slice(0, 11);
    return `${String(d.getDate()).padStart(2,'0')} ${m[d.getMonth()]} ${String(d.getFullYear()).slice(2)}`;
  }

  // ── Rolling history (data/history/<name>.json) ─────────────
  // Lazy-fetched per file, cached. Available names come from the manifest:
  //   fx, rates, macro, flows, regime  (see history.py)
  const _histCache = {};
  async function history(name) {
    if (_histCache[name] !== undefined) return _histCache[name];
    try {
      const r = await fetch(`data/history/${name}.json`, { cache: 'no-store' });
      _histCache[name] = r.ok ? await r.json() : null;
    } catch (e) {
      _histCache[name] = null;
    }
    return _histCache[name];
  }

  return { load, block, asOf, monthlyReturns, shortDate, PATH,
           narrative, narrativeBadge, changes, sourcesUsed,
           usdinr, asset, assetsPresent, sourceLinks, signals, history };
})();
