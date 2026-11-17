/* OpenTabular JavaScript client, served by `otab serve` at /sdk.js.
 *
 * A data app asks for measures by name and draws the rows it gets back:
 *
 *   const result = await otab.query({
 *     measures: ['Total Sales', 'Sales LY'],
 *     groupBy: ["'Date'[Year Month]"],
 *     filters: ["'Date'[Year] = 2024"],
 *   });
 *   otab.line(document.querySelector('#trend'), result);
 *   otab.inspect(document.querySelector('#inspect'), result);
 *
 * The page never builds SQL or DAX. `otab.inspect` shows, for every measure
 * on the page, its definition, owner, trust level, lineage and the SQL that ran.
 */
(function () {
  'use strict';

  // ---- transport -----------------------------------------------------------
  async function call(url, body) {
    const res = await fetch(url, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || res.statusText);
    return data;
  }

  // ---- formatting ----------------------------------------------------------
  // Understands the common Power BI format strings: #,##0  #,##0.00  0.0%  0
  function format(value, fmt) {
    if (value === null || value === undefined) return '–';
    if (typeof value !== 'number') return String(value);
    const f = fmt || '';
    const percent = f.includes('%');
    const decimals = f.includes('.') ? (f.split('.')[1].match(/[0#]/g) || []).length
      : (f || Number.isInteger(value)) ? 0 : 2;
    const grouped = !f || f.includes(',');
    const n = percent ? value * 100 : value;
    const text = n.toLocaleString('en-US', {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals, useGrouping: grouped,
    });
    return percent ? text + '%' : text;
  }

  function compact(value) {
    const a = Math.abs(value);
    if (a >= 1e9) return (value / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
    if (a >= 1e6) return (value / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (a >= 1e3) return (value / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return String(Math.round(value * 100) / 100);
  }

  // ---- small DOM helpers ---------------------------------------------------
  const SVG = 'http://www.w3.org/2000/svg';
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) el.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c !== null && c !== undefined) el.append(c);
    return el;
  }
  function s(tag, attrs, ...children) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    for (const c of children.flat()) if (c !== null && c !== undefined) el.append(c);
    return el;
  }
  function mount(el, node) {
    el.classList.add('otab-viz');
    el.replaceChildren(node);
  }
  function redrawOnResize(el, draw) {
    if (el._otabObserver) el._otabObserver.disconnect();
    let width = el.clientWidth;
    el._otabObserver = new ResizeObserver(() => {
      if (Math.abs(el.clientWidth - width) > 8) { width = el.clientWidth; draw(); }
    });
    el._otabObserver.observe(el);
  }

  let tip;
  function showTip(evt, rows, title) {
    if (!tip) { tip = h('div', { class: 'otab-tip otab-viz', role: 'status' }); document.body.append(tip); }
    tip.replaceChildren(
      title ? h('div', { class: 'otab-tip-title' }, title) : null,
      ...rows.map(r => h('div', { class: 'otab-tip-row' },
        r.color ? h('span', { class: 'otab-swatch', style: `background:${r.color}` }) : null,
        h('span', {}, r.label), h('b', {}, r.value))));
    tip.style.display = 'block';
    const pad = 14, w = tip.offsetWidth, hgt = tip.offsetHeight;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
    if (y + hgt > window.innerHeight - 8) y = evt.clientY - hgt - pad;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  function hideTip() { if (tip) tip.style.display = 'none'; }

  const measuresOf = r => r.columns.map((c, i) => ({ ...c, i })).filter(c => c.kind === 'measure');
  const dimsOf = r => r.columns.map((c, i) => ({ ...c, i })).filter(c => c.kind === 'dimension');
  const seriesColor = i => `var(--otab-series-${(i % 8) + 1})`;
  const pick = (cols, name) => name ? cols.find(c => c.name === name) : cols[0];

  // ---- table ---------------------------------------------------------------
  function table(el, result) {
    const head = h('tr', {}, result.columns.map(c =>
      h('th', { class: c.kind === 'measure' ? 'otab-num' : '', scope: 'col' }, c.name)));
    const body = result.rows.map(row => h('tr', {}, row.map((v, i) => {
      const c = result.columns[i];
      return h('td', { class: c.kind === 'measure' ? 'otab-num' : '' },
        c.kind === 'measure' ? format(v, c.format) : (v === null ? '(blank)' : String(v)));
    })));
    mount(el, h('div', { class: 'otab-table-wrap' },
      h('table', { class: 'otab-table' }, h('thead', {}, head), h('tbody', {}, body)),
      result.truncated ? h('p', { class: 'otab-note' }, `Showing the first ${result.rows.length} rows.`) : null));
  }

  // ---- horizontal bars: one measure across the members of one dimension ----
  function bars(el, result, opts = {}) {
    const dim = pick(dimsOf(result), opts.x), m = pick(measuresOf(result), opts.y);
    if (!dim || !m) return mount(el, h('p', { class: 'otab-note' }, 'A bar chart needs one dimension and one measure.'));
    const rows = result.rows.slice(0, opts.max || 25);
    const max = Math.max(0, ...rows.map(r => r[m.i] || 0)), min = Math.min(0, ...rows.map(r => r[m.i] || 0));
    const span = (max - min) || 1;
    const list = h('div', { class: 'otab-bars', role: 'list', 'aria-label': `${m.name} by ${dim.name}` },
      rows.map(r => {
        const v = r[m.i] || 0, label = r[dim.i] === null ? '(blank)' : String(r[dim.i]);
        const left = (Math.min(v, 0) - min) / span * 100, width = Math.abs(v) / span * 100;
        const row = h('div', { class: 'otab-bar-row', role: 'listitem', tabindex: opts.onSelect ? 0 : null },
          h('span', { class: 'otab-bar-label', title: label }, label),
          h('span', { class: 'otab-bar-track' },
            h('span', { class: 'otab-bar' + (v < 0 ? ' otab-neg' : ''), style: `left:${left}%;width:${width}%` })),
          h('span', { class: 'otab-bar-value' }, format(r[m.i], m.format)));
        row.addEventListener('mousemove', e => showTip(e, measuresOf(result).map(c => (
          { label: c.name, value: format(r[c.i], c.format) })), label));
        row.addEventListener('mouseleave', hideTip);
        if (opts.onSelect) {
          row.classList.add('otab-clickable');
          if (opts.selected !== undefined && opts.selected === r[dim.i]) row.classList.add('otab-selected');
          const go = () => opts.onSelect(r[dim.i], r);
          row.addEventListener('click', go);
          row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
        }
        return row;
      }));
    mount(el, h('div', {}, list,
      result.rows.length > rows.length ? h('p', { class: 'otab-note' }, `Top ${rows.length} of ${result.rows.length}.`) : null));
  }

  // ---- line chart: up to four measures over an ordered dimension -----------
  function line(el, result, opts = {}) {
    const dim = pick(dimsOf(result), opts.x);
    const all = measuresOf(result);
    const series = (opts.y ? opts.y.map(n => all.find(c => c.name === n)).filter(Boolean) : all).slice(0, 4);
    if (!dim || !series.length || result.rows.length < 2) {
      return mount(el, h('p', { class: 'otab-note' }, 'A line chart needs one dimension, one measure and two or more rows.'));
    }
    const draw = () => {
      const W = Math.max(320, el.clientWidth || 640), H = opts.height || 260;
      const m = { t: 12, r: series.length > 1 ? 112 : 20, b: 28, l: 48 };
      const rows = result.rows, n = rows.length;
      const vals = rows.flatMap(r => series.map(c => r[c.i])).filter(v => v !== null);
      let lo = Math.min(0, ...vals), hi = Math.max(...vals);
      if (hi === lo) hi = lo + 1;
      const step = niceStep((hi - lo) / 4);
      hi = Math.ceil(hi / step) * step; lo = Math.floor(lo / step) * step;
      const X = i => m.l + (W - m.l - m.r) * (n === 1 ? 0.5 : i / (n - 1));
      const Y = v => m.t + (H - m.t - m.b) * (1 - (v - lo) / (hi - lo));
      const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img',
        'aria-label': `${series.map(c => c.name).join(', ')} by ${dim.name}` });
      for (let v = lo; v <= hi + step / 2; v += step) {
        svg.append(s('line', { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), class: v === 0 ? 'otab-axis' : 'otab-grid' }));
        svg.append(s('text', { x: m.l - 8, y: Y(v) + 4, 'text-anchor': 'end', class: 'otab-tick' }, compact(v)));
      }
      const every = Math.ceil(n / Math.max(2, Math.floor((W - m.l - m.r) / 72)));
      rows.forEach((r, i) => {
        if (i % every === 0) svg.append(s('text', { x: X(i), y: H - 8, 'text-anchor': 'middle', class: 'otab-tick' }, String(r[dim.i])));
      });
      const ends = [];
      series.forEach((c, k) => {
        let d = '', pen = false, last = null;
        rows.forEach((r, i) => {
          const v = r[c.i];
          if (v === null) { pen = false; return; }
          d += `${pen ? 'L' : 'M'}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`; pen = true; last = { i, v };
        });
        svg.append(s('path', { d, fill: 'none', stroke: seriesColor(k), 'stroke-width': 2,
          'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
        if (last) ends.push({ c, k, y: Y(last.v), x: X(last.i) });
      });
      if (series.length > 1) {  // direct labels at the line ends, nudged apart
        ends.sort((a, b) => a.y - b.y);
        for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 14) ends[i].y = ends[i - 1].y + 14;
        ends.forEach(e => svg.append(s('text', { x: W - m.r + 8, y: e.y + 4, class: 'otab-end-label' }, e.c.name)));
      }
      const cross = s('line', { y1: m.t, y2: H - m.b, class: 'otab-cross', visibility: 'hidden' });
      const dots = series.map((c, k) => s('circle', { r: 4, fill: seriesColor(k), class: 'otab-dot', visibility: 'hidden' }));
      const hit = s('rect', { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: 'transparent' });
      hit.addEventListener('mousemove', e => {
        const box = svg.getBoundingClientRect();
        const px = (e.clientX - box.left) * (W / box.width);
        const i = Math.max(0, Math.min(n - 1, Math.round((px - m.l) / ((W - m.l - m.r) / Math.max(1, n - 1)))));
        cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i)); cross.setAttribute('visibility', 'visible');
        series.forEach((c, k) => {
          const v = rows[i][c.i];
          dots[k].setAttribute('visibility', v === null ? 'hidden' : 'visible');
          if (v !== null) { dots[k].setAttribute('cx', X(i)); dots[k].setAttribute('cy', Y(v)); }
        });
        showTip(e, series.map((c, k) => ({ label: c.name, value: format(rows[i][c.i], c.format), color: seriesColor(k) })), String(rows[i][dim.i]));
      });
      hit.addEventListener('mouseleave', () => {
        cross.setAttribute('visibility', 'hidden'); dots.forEach(d => d.setAttribute('visibility', 'hidden')); hideTip();
      });
      svg.append(cross, ...dots, hit);
      const legend = series.length > 1 ? h('div', { class: 'otab-legend' }, series.map((c, k) =>
        h('span', {}, h('span', { class: 'otab-swatch', style: `background:${seriesColor(k)}` }), c.name))) : null;
      mount(el, h('div', {}, legend, svg));
    };
    draw();
    redrawOnResize(el, draw);
  }
  function niceStep(raw) {
    const p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  }

  // ---- stat tile -----------------------------------------------------------
  function tile(el, { label, value, format: fmt, note }) {
    mount(el, h('div', { class: 'otab-tile' },
      h('div', { class: 'otab-tile-label' }, label),
      h('div', { class: 'otab-tile-value' }, format(value, fmt)),
      note ? h('div', { class: 'otab-tile-note' }, note) : null));
  }

  // ---- inspect: where every number on the page comes from ------------------
  const TRUST = {
    certified: ['✓', 'Certified', 'Defined in the model and endorsed by its owner.'],
    governed: ['●', 'Governed', 'Defined in the model and reviewed with it.'],
    'ad hoc': ['△', 'Ad hoc', 'Written in this query, not in the model.'],
  };
  function trustBadge(level) {
    const [glyph, label, hint] = TRUST[level] || TRUST.governed;
    return h('span', { class: 'otab-trust otab-trust-' + (level || 'governed').replace(' ', '-'), title: hint },
      h('span', { 'aria-hidden': 'true' }, glyph), ' ' + label);
  }
  async function inspect(el, result) {
    const cards = await Promise.all(measuresOf(result).map(async c => {
      if (c.trust === 'ad hoc') {
        return h('section', { class: 'otab-inspect-item' }, h('h4', {}, c.name, trustBadge(c.trust)),
          h('p', { class: 'otab-note' }, 'This measure was written in the query. Move it into the model to have it reviewed.'));
      }
      const info = await otab.explain(c.name);
      const lin = info.lineage;
      return h('section', { class: 'otab-inspect-item' },
        h('h4', {}, info.name, trustBadge(info.trust)),
        info.description ? h('p', {}, info.description) : null,
        h('dl', {},
          info.owner ? [h('dt', {}, 'Owner'), h('dd', {}, info.owner)] : null,
          lin.measures.length ? [h('dt', {}, 'Built on'), h('dd', {}, lin.measures.join(', '))] : null,
          h('dt', {}, 'Source columns'),
          h('dd', {}, lin.sources.flatMap(src => Object.values(src.columns).map(p => `${src.relation}.${p}`)).join(', ') || 'none')),
        h('pre', { class: 'otab-code' }, info.dax));
    }));
    mount(el, h('div', { class: 'otab-inspect' }, cards,
      h('details', {}, h('summary', {}, `SQL that ran${result.elapsed_ms !== undefined ? ` (${result.elapsed_ms} ms)` : ''}`),
        h('pre', { class: 'otab-code' }, result.sql))));
  }

  // ---- styles --------------------------------------------------------------
  const css = `
  .otab-viz{--otab-surface:#fcfcfb;--otab-text:#0b0b0b;--otab-text-2:#52514e;--otab-muted:#85847d;--otab-grid:#e6e5e0;
    --otab-axis:#b9b8b1;--otab-panel:#f2f1ed;--otab-series-1:#2a78d6;--otab-series-2:#eb6834;--otab-series-3:#1baf7a;
    --otab-series-4:#eda100;--otab-series-5:#e87ba4;--otab-series-6:#008300;--otab-series-7:#6250d6;--otab-series-8:#e34948;
    --otab-good:#0a7a3d;--otab-warn:#9a6200;color:var(--otab-text);font-variant-numeric:tabular-nums}
  @media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .otab-viz{--otab-surface:#1a1a19;--otab-text:#f0efec;
    --otab-text-2:#c3c2b7;--otab-muted:#8f8e86;--otab-grid:#31302d;--otab-axis:#55544f;--otab-panel:#252523;--otab-series-1:#3987e5;
    --otab-series-2:#d95926;--otab-series-3:#199e70;--otab-series-4:#c98500;--otab-series-5:#d55181;--otab-series-6:#0ca30c;
    --otab-series-7:#9085e9;--otab-series-8:#e66767;--otab-good:#4cc38a;--otab-warn:#e0a83a}}
  :root[data-theme="dark"] .otab-viz{--otab-surface:#1a1a19;--otab-text:#f0efec;--otab-text-2:#c3c2b7;--otab-muted:#8f8e86;
    --otab-grid:#31302d;--otab-axis:#55544f;--otab-panel:#252523;--otab-series-1:#3987e5;--otab-series-2:#d95926;
    --otab-series-3:#199e70;--otab-series-4:#c98500;--otab-series-5:#d55181;--otab-series-6:#0ca30c;--otab-series-7:#9085e9;
    --otab-series-8:#e66767;--otab-good:#4cc38a;--otab-warn:#e0a83a}
  .otab-note{color:var(--otab-text-2);font-size:13px;margin:8px 0 0}
  .otab-table-wrap{overflow:auto;max-height:420px}
  .otab-table{border-collapse:collapse;width:100%;font-size:13.5px}
  .otab-table th{position:sticky;top:0;background:var(--otab-surface);text-align:left;font-weight:600;
    border-bottom:1px solid var(--otab-axis);padding:7px 12px 7px 0;white-space:nowrap}
  .otab-table td{border-bottom:1px solid var(--otab-grid);padding:6px 12px 6px 0}
  .otab-table .otab-num{text-align:right;padding-right:0;padding-left:12px}
  .otab-bars{display:grid;gap:2px}
  .otab-bar-row{display:grid;grid-template-columns:minmax(72px,24%) 1fr minmax(64px,auto);gap:12px;align-items:center;
    padding:4px 6px;border-radius:6px;font-size:13.5px}
  .otab-bar-row:hover,.otab-bar-row.otab-selected{background:var(--otab-panel)}
  .otab-bar-row.otab-clickable{cursor:pointer}
  .otab-bar-row:focus-visible{outline:2px solid var(--otab-series-1);outline-offset:1px}
  .otab-bar-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .otab-bar-track{position:relative;height:14px}
  .otab-bar{position:absolute;top:0;bottom:0;background:var(--otab-series-1);border-radius:0 4px 4px 0;min-width:1px}
  .otab-bar.otab-neg{border-radius:4px 0 0 4px}
  .otab-bar-value{text-align:right;color:var(--otab-text)}
  .otab-grid{stroke:var(--otab-grid);stroke-width:1}.otab-axis{stroke:var(--otab-axis);stroke-width:1}
  .otab-tick{fill:var(--otab-text-2);font-size:11.5px}.otab-end-label{fill:var(--otab-text);font-size:12px}
  .otab-cross{stroke:var(--otab-axis);stroke-width:1}.otab-dot{stroke:var(--otab-surface);stroke-width:2}
  .otab-legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--otab-text-2);margin-bottom:6px}
  .otab-swatch{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}
  .otab-tip{position:fixed;display:none;z-index:1000;pointer-events:none;background:var(--otab-surface);
    border:1px solid var(--otab-axis);border-radius:8px;padding:8px 10px;font-size:12.5px;min-width:140px;
    box-shadow:0 6px 18px rgba(20,20,18,.14)}
  .otab-tip-title{font-weight:600;margin-bottom:4px}
  .otab-tip-row{display:flex;align-items:center;gap:6px}.otab-tip-row b{margin-left:auto;padding-left:14px;font-weight:600}
  .otab-tile-label{font-size:13px;color:var(--otab-text-2)}
  .otab-tile-value{font-size:30px;font-weight:600;letter-spacing:-.01em;line-height:1.2;margin-top:2px}
  .otab-tile-note{font-size:12.5px;color:var(--otab-text-2);margin-top:2px}
  .otab-inspect{font-size:13.5px;display:grid;grid-template-columns:minmax(0,1fr);gap:14px}
  .otab-inspect-item{border-top:1px solid var(--otab-grid);padding-top:12px}
  .otab-inspect-item:first-child{border-top:0;padding-top:0}
  .otab-inspect h4{margin:0 0 4px;font-size:14px;font-weight:600;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
  .otab-inspect p{margin:0 0 6px;color:var(--otab-text-2)}
  .otab-inspect dl{display:grid;grid-template-columns:auto minmax(0,1fr);gap:2px 12px;margin:0 0 8px}
  .otab-inspect dt{color:var(--otab-text-2)}.otab-inspect dd{margin:0;overflow-wrap:anywhere}
  .otab-inspect summary{cursor:pointer;color:var(--otab-text-2)}
  .otab-code{background:var(--otab-panel);border-radius:6px;padding:10px 12px;margin:6px 0 0;overflow:auto;max-height:280px;
    font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre}
  .otab-inspect-item .otab-code{white-space:pre-wrap;overflow-wrap:anywhere}
  .otab-trust{font-size:11.5px;font-weight:600;padding:1px 8px;border-radius:999px;border:1px solid currentColor;white-space:nowrap}
  .otab-trust-certified,.otab-trust-governed{color:var(--otab-good)}.otab-trust-ad-hoc{color:var(--otab-warn)}`;
  document.head.append(h('style', { 'data-otab': '' }, css));

  const otab = {
    model: () => call('/api/model'),
    explain: name => call('/api/measures/' + encodeURIComponent(name)),
    search: text => call('/api/search?q=' + encodeURIComponent(text)),
    query: q => call('/api/query', q),
    records: r => r.rows.map(row => Object.fromEntries(row.map((v, i) => [r.columns[i].name, v]))),
    format, table, bars, line, tile, inspect, trustBadge,
  };
  window.otab = otab;
})();
