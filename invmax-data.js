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

  // small helper: format an ISO-ish date to "01 Aug 26"
  function shortDate(s) {
    if (!s) return '';
    const m = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const d = new Date(s);
    if (isNaN(d)) return String(s).slice(0, 11);
    return `${String(d.getDate()).padStart(2,'0')} ${m[d.getMonth()]} ${String(d.getFullYear()).slice(2)}`;
  }

  return { load, block, asOf, monthlyReturns, shortDate, PATH,
           narrative, narrativeBadge, changes, sourcesUsed };
})();
