// trade.js — main trading overview page

async function renderTradePage() {
  const app = document.getElementById('app');
  app.innerHTML = createSkeleton();

  // Pre-load ticker name map for current market so first paint of trades/holdings
  // already shows "600522.SH 山西汾酒". Non-blocking — fall through if it fails.
  loadTickerNames(state.market).catch(() => {});

  try {
    const summary = await api('/trade/summary');
    renderHero(summary);
  } catch (e) {
    renderHeroFallback();
  }

  try {
    const eqData = await api('/trade/equity-curves');
    renderEquityCurves(eqData);
  } catch (e) {
    document.querySelector('.chart-section')?.remove();
  }

  try {
    const accounts = await api('/trade/accounts');
    renderAccountCards(accounts);
  } catch (e) {
    console.warn('Failed to load accounts', e);
  }
}

function renderHero(s) {
  const app = document.getElementById('app');
  const d = s.distribution || {};
  const medCls = (d.median_pct || 0) >= 0 ? 'positive' : 'negative';
  const medSign = (d.median_pct || 0) >= 0 ? '+' : '';
  const bestCls = d.best && d.best.pnl_pct >= 0 ? 'positive' : 'negative';
  const worstCls = d.worst && d.worst.pnl_pct >= 0 ? 'positive' : 'negative';
  const heroHtml = `
    ${typeof eventsSectionHtml === 'function' ? eventsSectionHtml() : ''}
    <div class="hero fade-in">
      <div class="hero-label">${t('dist_title')}</div>
      <div class="hero-value ${medCls}" id="hero-median">
        ${medSign}${(d.median_pct || 0).toFixed(2)}%
      </div>
      <div class="hero-pnl" style="font-size:1rem;opacity:0.85;">
        ${t('dist_median')} across ${d.count || 0} accounts${d.retired_count ? ` <span style="opacity:0.6;">(+${d.retired_count} ${t('retired_label') || 'retired'})</span>` : ''}
        &nbsp;·&nbsp; ${t('dist_win_rate')}: ${d.win_rate || 0}% (${d.win_count || 0}/${d.count || 0})
      </div>
      <div class="stats-row">
        <div class="glass-card stat-box">
          <div class="stat-label">${t('dist_best')}</div>
          <div class="stat-value ${bestCls}">
            ${d.best ? (d.best.pnl_pct >= 0 ? '+' : '') + formatPercent(d.best.pnl_pct) : '—'}
          </div>
          <div class="stat-label" style="font-size:0.7rem;opacity:0.7;">
            ${d.best ? d.best.account_id : ''}
          </div>
        </div>
        <div class="glass-card stat-box">
          <div class="stat-label">${t('dist_worst')}</div>
          <div class="stat-value ${worstCls}">
            ${d.worst ? (d.worst.pnl_pct >= 0 ? '+' : '') + formatPercent(d.worst.pnl_pct) : '—'}
          </div>
          <div class="stat-label" style="font-size:0.7rem;opacity:0.7;">
            ${d.worst ? d.worst.account_id : ''}
          </div>
        </div>
        <div class="glass-card stat-box">
          <div class="stat-label">${t('dist_iqr')} (Q1–Q3)</div>
          <div class="stat-value" style="font-size:1.1rem;">
            ${formatPercent(d.q1_pct || 0)} ~ ${formatPercent(d.q3_pct || 0)}
          </div>
        </div>
      </div>
    </div>
    <div class="section">
      <div class="section-title">${t('dist_hist_title')}</div>
      <div class="glass-card" id="pnl-histogram" style="padding:20px;"></div>
    </div>
    <div class="section chart-section">
      <div class="section-title">${t('equity_curve')}</div>
      <div class="glass-card chart-container" id="equity-chart-container">
        <div class="chart-tooltip" id="chart-tooltip">
          <div class="tooltip-name"></div>
          <div class="tooltip-value"></div>
        </div>
        <div id="equity-chart" style="height:420px;"></div>
      </div>
    </div>
    <div class="section">
      <div class="section-title-row">
        <div class="section-title">${t('accounts_overview')}</div>
        <div class="sort-controls" id="sort-controls">
          <label class="sort-label">${t('sort_by')}</label>
          <select id="sort-select" class="sort-select">
            <option value="pnl_pct_desc">${t('sort_pnl_desc')}</option>
            <option value="pnl_pct_asc">${t('sort_pnl_asc')}</option>
            <option value="name_asc">${t('sort_name_asc')}</option>
            <option value="name_desc">${t('sort_name_desc')}</option>
            <option value="trade_count_desc">${t('sort_trades_desc')}</option>
            <option value="trade_count_asc">${t('sort_trades_asc')}</option>
            <option value="sharpe_desc">${t('sort_sharpe_desc')}</option>
            <option value="sharpe_asc">${t('sort_sharpe_asc')}</option>
          </select>
        </div>
      </div>
      <div class="account-tabs" id="account-tabs" role="tablist">
        <button class="account-tab active" data-tab="active" role="tab">${t('tab_active') || 'Active'} <span class="tab-count" id="tab-count-active">0</span></button>
        <button class="account-tab" data-tab="retired" role="tab">${t('tab_retired') || 'Retired'} <span class="tab-count" id="tab-count-retired">0</span></button>
      </div>
      <div class="accounts-grid" id="accounts-grid"></div>
      <div class="tombstone-wall" id="tombstone-wall" style="display:none;"></div>
    </div>
  `;
  app.innerHTML = heroHtml;
  renderPnlHistogram(d);
  if (typeof startEventsStream === 'function') startEventsStream();
}

// Per-account PnL% histogram — independent $10k accounts → sum is meaningless,
// distribution across the 20 accounts is what tells us strategy dispersion.
function renderPnlHistogram(dist) {
  const host = document.getElementById('pnl-histogram');
  if (!host || !dist || !dist.accounts || !dist.accounts.length) {
    if (host) host.innerHTML = `<div style="opacity:0.6;">No data</div>`;
    return;
  }
  // Retired accounts are frozen; their stale PnL shouldn't skew the live
  // distribution. API already excludes them from median/IQR/best/worst —
  // exclude here too so bin counts match the headline numbers.
  const accounts = dist.accounts.filter(a => (a.status || 'active') !== 'retired');
  if (!accounts.length) {
    host.innerHTML = `<div style="opacity:0.6;">No active accounts</div>`;
    return;
  }
  const pcts = accounts.map(a => a.pnl_pct);
  const minV = Math.min(...pcts, 0);
  const maxV = Math.max(...pcts, 0);
  const pad = Math.max(0.5, (maxV - minV) * 0.08);
  const lo = minV - pad, hi = maxV + pad;
  const N_BINS = 12;
  const binW = (hi - lo) / N_BINS;
  const bins = new Array(N_BINS).fill(null).map(() => ({ a: [], b: [], q: [], bench: [] }));
  accounts.forEach(acc => {
    let idx = Math.floor((acc.pnl_pct - lo) / binW);
    if (idx < 0) idx = 0;
    if (idx >= N_BINS) idx = N_BINS - 1;
    const aid = acc.account_id || '';
    if (aid.startsWith('IDX')) bins[idx].bench.push(acc);
    // CN prefix 'C' on Q-accounts (CQ01) must match Q before A.
    else if (/^C?Q\d/.test(aid)) bins[idx].q.push(acc);
    else if (/^C?A\d/.test(aid)) bins[idx].a.push(acc);
    else bins[idx].b.push(acc);
  });
  const maxCount = Math.max(1, ...bins.map(b => b.a.length + b.b.length + b.q.length + b.bench.length));

  const W = host.clientWidth || 800;
  const H = 220;
  const ML = 44, MR = 16, MT = 16, MB = 36;
  const plotW = W - ML - MR, plotH = H - MT - MB;
  const xOf = v => ML + ((v - lo) / (hi - lo)) * plotW;
  const yOf = c => MT + plotH - (c / maxCount) * plotH;

  // x-axis ticks
  const ticks = [];
  for (let i = 0; i <= 6; i++) {
    const v = lo + (hi - lo) * (i / 6);
    ticks.push(v);
  }
  // Zero line
  const zeroX = (lo <= 0 && hi >= 0) ? xOf(0) : null;

  const colA = '#4da6ff', colB = '#b388ff', colQ = '#34d399', colBench = '#ffb74d';
  let svg = `<svg width="${W}" height="${H}" style="display:block;">`;
  // y-grid
  for (let i = 0; i <= 4; i++) {
    const y = MT + plotH * (i / 4);
    const label = Math.round(maxCount * (1 - i / 4));
    svg += `<line x1="${ML}" y1="${y}" x2="${W - MR}" y2="${y}" stroke="rgba(255,255,255,0.05)"/>`;
    svg += `<text x="${ML - 6}" y="${y + 3}" fill="rgba(255,255,255,0.4)" font-size="10" text-anchor="end">${label}</text>`;
  }
  if (zeroX !== null) {
    svg += `<line x1="${zeroX}" y1="${MT}" x2="${zeroX}" y2="${MT + plotH}" stroke="rgba(255,255,255,0.25)" stroke-dasharray="3,3"/>`;
  }
  // bars — stacked A, B, bench. Attach bin-index so mouse handler can look up accounts.
  const gap = Math.max(1, (plotW / N_BINS) * 0.15);
  bins.forEach((b, i) => {
    const x0 = xOf(lo + i * binW) + gap / 2;
    const bw = Math.max(1, (plotW / N_BINS) - gap);
    const binLo = lo + i * binW;
    const binHi = lo + (i + 1) * binW;
    let stackTop = MT + plotH;
    [['a', b.a, colA], ['b', b.b, colB], ['q', b.q, colQ], ['bench', b.bench, colBench]].forEach(([k, arr, color]) => {
      if (!arr.length) return;
      const h = (arr.length / maxCount) * plotH;
      stackTop -= h;
      svg += `<rect class="pnl-bar" x="${x0}" y="${stackTop}" width="${bw}" height="${h}" fill="${color}" opacity="0.85" data-bin="${i}" data-range="${binLo.toFixed(2)},${binHi.toFixed(2)}" style="cursor:pointer;"/>`;
    });
    // Transparent hover-capture rect covering full bin column (so gaps between stacks still trigger tooltip)
    const totalH = ((b.a.length + b.b.length + b.q.length + b.bench.length) / maxCount) * plotH;
    if (totalH > 0) {
      svg += `<rect class="pnl-bar-hover" x="${x0}" y="${MT + plotH - totalH}" width="${bw}" height="${totalH}" fill="transparent" data-bin="${i}" data-range="${binLo.toFixed(2)},${binHi.toFixed(2)}" style="cursor:pointer;"/>`;
    }
  });
  // x-axis labels
  ticks.forEach(v => {
    const x = xOf(v);
    svg += `<text x="${x}" y="${H - MB + 18}" fill="rgba(255,255,255,0.5)" font-size="10" text-anchor="middle">${v >= 0 ? '+' : ''}${v.toFixed(1)}%</text>`;
  });
  // median / mean markers
  if (typeof dist.median_pct === 'number') {
    const mx = xOf(dist.median_pct);
    svg += `<line x1="${mx}" y1="${MT}" x2="${mx}" y2="${MT + plotH}" stroke="#ffffff" stroke-width="1.5" stroke-dasharray="4,3" opacity="0.7"/>`;
    svg += `<text x="${mx + 4}" y="${MT + 12}" fill="#ffffff" font-size="10" opacity="0.8">${t('dist_median')} ${dist.median_pct >= 0 ? '+' : ''}${dist.median_pct.toFixed(2)}%</text>`;
  }
  svg += `</svg>`;
  // legend
  const legend = `
    <div style="display:flex;gap:16px;margin-top:10px;flex-wrap:wrap;font-size:0.8rem;opacity:0.85;">
      <span><span style="display:inline-block;width:12px;height:12px;background:${colA};border-radius:2px;vertical-align:middle;"></span> Group A (Alpha158)</span>
      <span><span style="display:inline-block;width:12px;height:12px;background:${colB};border-radius:2px;vertical-align:middle;"></span> Group B (GP)</span>
      <span><span style="display:inline-block;width:12px;height:12px;background:${colQ};border-radius:2px;vertical-align:middle;"></span> Group Q (Qlib ML)</span>
      <span><span style="display:inline-block;width:12px;height:12px;background:${colBench};border-radius:2px;vertical-align:middle;"></span> Benchmarks (IDX)</span>
    </div>`;
  host.innerHTML = svg + legend;

  // Floating HTML tooltip for bin hover — lists each account with strategy & pnl%.
  let tip = host.querySelector('.pnl-hist-tooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'pnl-hist-tooltip';
    tip.style.cssText = `
      position:fixed; pointer-events:none; z-index:9999;
      background:rgba(15,18,28,0.96); border:1px solid rgba(255,255,255,0.15);
      border-radius:8px; padding:10px 12px; font-size:12px; line-height:1.5;
      color:#eaeaf0; box-shadow:0 8px 24px rgba(0,0,0,0.5);
      max-width:320px; display:none; backdrop-filter:blur(8px);
    `;
    document.body.appendChild(tip);
  }
  const binsIndex = bins;
  const showTip = (ev, el) => {
    const idx = Number(el.getAttribute('data-bin'));
    const rng = (el.getAttribute('data-range') || '').split(',');
    const b = binsIndex[idx];
    if (!b) return;
    const all = [...b.a, ...b.b, ...b.q, ...b.bench].sort((a, z) => z.pnl_pct - a.pnl_pct);
    const rows = all.map(a => {
      const cls = a.pnl_pct >= 0 ? 'color:#4ade80;' : 'color:#f87171;';
      const sign = a.pnl_pct >= 0 ? '+' : '';
      const strat = a.strategy_name ? ` <span style="opacity:0.6;">${a.strategy_name}</span>` : '';
      return `<div style="display:flex;justify-content:space-between;gap:12px;">
        <span><b>${a.account_id}</b>${strat}</span>
        <span style="${cls}font-variant-numeric:tabular-nums;">${sign}${a.pnl_pct.toFixed(2)}%</span>
      </div>`;
    }).join('');
    tip.innerHTML = `
      <div style="opacity:0.7;margin-bottom:6px;font-size:11px;">
        Bin: ${rng[0]}% ~ ${rng[1]}% &nbsp;·&nbsp; ${all.length} account${all.length>1?'s':''}
      </div>${rows}`;
    tip.style.display = 'block';
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + tw > window.innerWidth - 8) x = ev.clientX - tw - 14;
    if (y + th > window.innerHeight - 8) y = ev.clientY - th - 14;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  };
  const hideTip = () => { tip.style.display = 'none'; };
  host.querySelectorAll('.pnl-bar, .pnl-bar-hover').forEach(el => {
    el.addEventListener('mousemove', ev => showTip(ev, el));
    el.addEventListener('mouseleave', hideTip);
  });
}

function renderHeroFallback() {
  const app = document.getElementById('app');
  app.innerHTML = `
    ${typeof eventsSectionHtml === 'function' ? eventsSectionHtml() : ''}
    <div class="hero fade-in">
      <div class="hero-label">${t('dist_title')}</div>
      <div class="hero-value">—</div>
      <div class="hero-pnl" style="color:var(--text-secondary);">${t('cannot_connect')}</div>
    </div>
    <div class="section chart-section">
      <div class="section-title">${t('equity_curve')}</div>
      <div class="glass-card chart-container" id="equity-chart-container">
        <div class="chart-tooltip" id="chart-tooltip"><div class="tooltip-name"></div><div class="tooltip-value"></div></div>
        <div id="equity-chart" style="height:420px;"></div>
      </div>
    </div>
    <div class="section">
      <div class="section-title">${t('accounts_overview')}</div>
      <div class="accounts-grid" id="accounts-grid"></div>
      <div class="tombstone-wall" id="tombstone-wall" style="display:none;"></div>
    </div>
  `;
  if (typeof startEventsStream === 'function') startEventsStream();
}

function renderEquityCurves(data) {
  const container = document.getElementById('equity-chart');
  if (!container || !window.LightweightCharts) return;

  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 420,
    layout: { background: { type: 'solid', color: 'transparent' }, textColor: 'rgba(0,0,0,0.65)', fontSize: 12 },
    grid: { vertLines: { color: 'rgba(0,0,0,0.06)' }, horzLines: { color: 'rgba(0,0,0,0.06)' } },
    crosshair: { mode: 0, vertLine: { color: 'rgba(255,255,255,0.1)', width: 1 }, horzLine: { color: 'rgba(255,255,255,0.1)', width: 1 } },
    rightPriceScale: { borderColor: 'rgba(0,0,0,0.12)' },
    timeScale: { borderColor: 'rgba(0,0,0,0.12)', timeVisible: true, secondsVisible: false, rightOffset: 6, barSpacing: 10 },
    handleScroll: true,
    handleScale: true,
  });

  const seriesMap = {};
  const aColors = ['#0088ff','#00aaff','#00bbee','#0099dd','#00ccff','#1199ee','#2288dd','#00aacc','#0077ee','#0066dd'];
  const bColors = ['#7b2ff7','#9b59f7','#b388ff','#c77dff','#a855f7','#8b5cf6','#7c3aed','#9333ea','#a855f7','#b06cff'];
  const benchColors = {
    QQQ: '#ffb74d', SPY: '#81c784',
    // CN: equity-curves API returns 'IDX3' (not '沪深300'). Match it here so
    // IDX3 renders as a dashed yellow benchmark line — same styling as QQQ.
    IDX3: '#ffb74d',
    '沪深300': '#ffb74d', 'CSI300': '#ffb74d',
  };

  // API now returns {curves: {name: [...]}, meta: {name: {status, retired_at, retire_reason}}}
  // Backward-compat: if the top-level is already a {name: [...]} dict (legacy
  // shape), treat the whole payload as `curves` with empty meta.
  let curvesObj, curvesMeta;
  if (data && data.curves && typeof data.curves === 'object') {
    curvesObj = data.curves;
    curvesMeta = data.meta || {};
  } else {
    curvesObj = data;
    curvesMeta = {};
  }
  const curveEntries = Array.isArray(curvesObj)
    ? curvesObj
    : Object.entries(curvesObj).map(([name, pts]) => ({name, data: pts}));
  // Sort so benchmarks render LAST (on top of other lines)
  curveEntries.sort((a, b) => {
    const aa = benchColors[a.name] ? 1 : 0;
    const bb = benchColors[b.name] ? 1 : 0;
    return aa - bb;
  });
  curveEntries.forEach((curve, i) => {
    const name = curve.name || '';
    const isBench = !!benchColors[name];
    const isA = !isBench && name.startsWith('A');
    const meta = curvesMeta[name] || {};
    const isRetired = meta.status === 'retired';
    let color, lineWidth = 1, lineStyle = 0;
    if (isBench) {
      color = benchColors[name];
      lineWidth = 3;
      lineStyle = 2;   // dashed
    } else if (isRetired) {
      // Retired curve: gray dashed, thin — visually de-emphasised but still
      // present so user can see the locked-in equity history. The series
      // data is server-truncated at retired_at so the line stops there.
      color = 'rgba(180,180,180,0.55)';
      lineWidth = 1;
      lineStyle = 1;   // dotted
    } else {
      const palette = isA ? aColors : bColors;
      color = palette[i % palette.length];
    }
    const series = chart.addLineSeries({
      color,
      lineWidth,
      lineStyle,
      priceLineVisible: false,
      lastValueVisible: isBench,
      crosshairMarkerVisible: !isRetired,
      title: isBench ? name : (isRetired ? `${name} (${t('retired_label') || 'retired'})` : undefined),
    });
    if (curve.data && curve.data.length) {
      const mapped = curve.data.map(d => {
        const ts = d.timestamp || d.time || d.date;
        const epoch = Math.floor(new Date(ts).getTime() / 1000);
        return { time: epoch, value: d.equity || d.value };
      }).filter(p => !isNaN(p.time) && p.value != null);
      // deduplicate by time (keep last), sort ascending
      const byTime = {};
      mapped.forEach(p => byTime[p.time] = p.value);
      const final = Object.entries(byTime).sort((a,b) => a[0]-b[0]).map(([time,value]) => ({time: Number(time), value}));
      if (final.length) series.setData(final);
    }
    seriesMap[curve.name] = { series, color, data: curve.data, isBench, baseWidth: lineWidth, baseStyle: lineStyle };
  });

  chart.timeScale().fitContent();

  // Persistent legend for benchmarks (top-right overlay) — market-aware.
  const benchLegend = document.createElement('div');
  benchLegend.className = 'equity-bench-legend';
  if (state.market === 'CN') {
    benchLegend.innerHTML = `
      <div class="lg-row"><span class="lg-swatch" style="background:#ffb74d;"></span>沪深300 <span class="lg-hint">${t('bench_suffix')}</span></div>
    `;
  } else {
    benchLegend.innerHTML = `
      <div class="lg-row"><span class="lg-swatch" style="background:#ffb74d;"></span>${t('bench_qqq')} <span class="lg-hint">${t('bench_suffix')}</span></div>
      <div class="lg-row"><span class="lg-swatch" style="background:#81c784;"></span>${t('bench_spy')} <span class="lg-hint">${t('bench_suffix')}</span></div>
    `;
  }
  container.appendChild(benchLegend);

  // Tooltip on crosshair move
  const tooltip = document.getElementById('chart-tooltip');
  chart.subscribeCrosshairMove(param => {
    if (!param.time || !param.seriesData || param.seriesData.size === 0) {
      tooltip.classList.remove('visible');
      Object.values(seriesMap).forEach(s =>
        s.series.applyOptions({ color: s.color, lineWidth: s.baseWidth, lineStyle: s.baseStyle })
      );
      return;
    }
    // find hovered (highest value near mouse)
    let best = null, bestVal = -Infinity;
    param.seriesData.forEach((val, series) => {
      const v = val.value;
      if (v !== undefined) {
        const name = Object.keys(seriesMap).find(k => seriesMap[k].series === series);
        if (name) { if (!best || Math.abs(v) > Math.abs(bestVal)) { best = name; bestVal = v; } }
      }
    });
    if (best) {
      tooltip.querySelector('.tooltip-name').textContent = best;
      tooltip.querySelector('.tooltip-value').textContent = formatCurrency(bestVal);
      tooltip.classList.add('visible');
      Object.entries(seriesMap).forEach(([name, s]) => {
        if (s.isBench) {
          // benchmarks always stay visible
          s.series.applyOptions({ lineWidth: s.baseWidth, lineStyle: s.baseStyle });
        } else if (name === best) {
          s.series.applyOptions({ lineWidth: 2.5 });
        } else {
          s.series.applyOptions({ lineWidth: 0.5 });
        }
      });
    }
  });

  // resize
  new ResizeObserver(() => chart.applyOptions({ width: container.clientWidth })).observe(container);
}

let _accountsCache = null;

function sortAccounts(list, mode) {
  const arr = [...list];
  const byGroupThenId = (a, b) => {
    const ga = (a.account_id || '').charAt(0);
    const gb = (b.account_id || '').charAt(0);
    if (ga !== gb) return ga.localeCompare(gb);
    return (a.account_id || '').localeCompare(b.account_id || '');
  };
  const cmpNum = (key, desc) => (a, b) => {
    const va = Number(a[key] ?? 0), vb = Number(b[key] ?? 0);
    if (va === vb) return byGroupThenId(a, b);
    return desc ? vb - va : va - vb;
  };
  switch (mode) {
    case 'pnl_pct_desc': arr.sort(cmpNum('pnl_pct', true)); break;
    case 'pnl_pct_asc':  arr.sort(cmpNum('pnl_pct', false)); break;
    case 'name_asc':     arr.sort((a,b) => (a.account_id||'').localeCompare(b.account_id||'')); break;
    case 'name_desc':    arr.sort((a,b) => (b.account_id||'').localeCompare(a.account_id||'')); break;
    case 'trade_count_desc': arr.sort(cmpNum('trade_count', true)); break;
    case 'trade_count_asc':  arr.sort(cmpNum('trade_count', false)); break;
    case 'sharpe_desc':  arr.sort(cmpNum('sharpe_ratio', true)); break;
    case 'sharpe_asc':   arr.sort(cmpNum('sharpe_ratio', false)); break;
    default: arr.sort(cmpNum('pnl_pct', true));
  }
  return arr;
}

function renderAccountCards(data) {
  const grid = document.getElementById('accounts-grid');
  if (!grid) return;
  const accounts = data.accounts || data;
  // Split active vs retired so the main grid only shows actively-trading
  // accounts. Retired ones are stashed behind a toggle (count badge) so the
  // user can still inspect them without polluting headline metrics.
  _accountsCache = accounts.filter(a => (a.status || 'active') !== 'retired');
  _retiredCache  = accounts.filter(a => (a.status || 'active') === 'retired');

  const select = document.getElementById('sort-select');
  const saved = localStorage.getItem('accounts_sort_mode') || 'pnl_pct_desc';
  if (select) {
    select.value = saved;
    if (!select._bound) {
      select.addEventListener('change', () => {
        localStorage.setItem('accounts_sort_mode', select.value);
        paintAccounts(select.value);
      });
      select._bound = true;
    }
  }
  paintAccounts(saved);
  setupAccountTabs();
  paintTombstones();
}

let _retiredCache = [];
let _activeTab = 'active';

function setupAccountTabs() {
  const tabs = document.getElementById('account-tabs');
  if (!tabs || tabs._bound) return;
  // counts
  const ca = document.getElementById('tab-count-active');
  const cr = document.getElementById('tab-count-retired');
  if (ca) ca.textContent = _accountsCache.length;
  if (cr) cr.textContent = _retiredCache.length;
  tabs.querySelectorAll('.account-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      _activeTab = tab;
      tabs.querySelectorAll('.account-tab').forEach(b => b.classList.toggle('active', b === btn));
      const grid = document.getElementById('accounts-grid');
      const wall = document.getElementById('tombstone-wall');
      const sortCtrl = document.getElementById('sort-controls');
      if (tab === 'retired') {
        if (grid) grid.style.display = 'none';
        if (wall) wall.style.display = '';
        if (sortCtrl) sortCtrl.style.visibility = 'hidden';
        paintTombstones();
      } else {
        if (grid) grid.style.display = '';
        if (wall) wall.style.display = 'none';
        if (sortCtrl) sortCtrl.style.visibility = '';
      }
    });
  });
  tabs._bound = true;
}

function paintTombstones() {
  const wall = document.getElementById('tombstone-wall');
  if (!wall) return;
  if (!_retiredCache.length) {
    wall.innerHTML = `<div class="tombstone-empty">${t('tomb_empty') || 'No retired accounts. May they all live long.'}</div>`;
    return;
  }
  // Sort: most recent retirements first
  const sorted = _retiredCache.slice().sort((a, b) => (b.retired_at || '').localeCompare(a.retired_at || ''));
  wall.innerHTML = sorted.map(a => tombstoneHtml(a)).join('');
  wall.querySelectorAll('.tombstone').forEach(el => {
    el.addEventListener('click', () => openTombstoneModal(el.dataset.id));
  });
}

function tombstoneHtml(a) {
  const id = a.account_id || a.id;
  const pnlPct = a.pnl_pct || 0;
  const sign = pnlPct >= 0 ? '+' : '';
  const pnlCls = pnlPct >= 0 ? 'positive' : 'negative';
  const born = (a.created_at || '').slice(0, 10) || '—';
  const died = (a.retired_at || '').slice(0, 10) || '—';
  const reason = (a.retire_reason || t('retired_tooltip') || '').replace(/"/g, '&quot;');
  const strat = a.strategy_name ? tStrategy(a.strategy_name, id) : '';
  return `
    <div class="tombstone fade-in" data-id="${id}" title="${reason}">
      <div class="tombstone-cross">✝</div>
      <div class="tombstone-rip">R.I.P.</div>
      <div class="tombstone-id">${id}</div>
      <div class="tombstone-strat">${strat}</div>
      <div class="tombstone-dates">${born} — ${died}</div>
      <div class="tombstone-return ${pnlCls}">${sign}${formatPercent(pnlPct)}</div>
      <div class="tombstone-epitaph">${(a.retire_reason || '').slice(0, 60) || (t('retired_tooltip') || '')}</div>
    </div>`;
}

async function openTombstoneModal(accountId) {
  let modal = document.getElementById('tombstone-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'tombstone-modal';
    modal.className = 'tombstone-modal';
    modal.innerHTML = `<div class="tombstone-modal-backdrop"></div><div class="tombstone-modal-body glass-card"></div>`;
    document.body.appendChild(modal);
    modal.querySelector('.tombstone-modal-backdrop').addEventListener('click', () => modal.classList.remove('open'));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') modal.classList.remove('open'); });
  }
  const body = modal.querySelector('.tombstone-modal-body');
  body.innerHTML = `<div class="tombstone-modal-loading">${t('events_loading') || 'Loading…'}</div>`;
  modal.classList.add('open');
  try {
    const [accData, factors] = await Promise.all([
      api(`/trade/account/${accountId}`),
      api(`/factors/${accountId}`).catch(() => ({ factors: [] })),
    ]);
    renderTombstoneModal(body, accountId, accData, factors);
  } catch (e) {
    body.innerHTML = `<button class="tombstone-modal-close" aria-label="Close">×</button><p style="color:var(--negative);padding:24px;">${t('load_failed')} ${e.message}</p>`;
    body.querySelector('.tombstone-modal-close').addEventListener('click', () => modal.classList.remove('open'));
  }
}

function renderTombstoneModal(body, accountId, accData, factors) {
  const meta = accData.meta || {};
  const trades = accData.trades || [];
  const positions = accData.positions || [];
  const equityCurve = accData.equity_curve || [];
  const finalEquity = equityCurve.length ? equityCurve[equityCurve.length - 1].equity : (accData.state?.equity);
  const initialCash = meta.initial_cash || (accData.market === 'CN' ? 100000 : 10000);
  const lifetimeRet = initialCash ? ((finalEquity / initialCash) - 1) * 100 : 0;
  const sign = lifetimeRet >= 0 ? '+' : '';
  const retCls = lifetimeRet >= 0 ? 'positive' : 'negative';
  const born = (meta.created_at || '').slice(0, 10) || '—';
  const died = (meta.retired_at || '').slice(0, 10) || '—';
  const lifeDays = (meta.created_at && meta.retired_at)
    ? Math.max(1, Math.round((new Date(meta.retired_at) - new Date(meta.created_at)) / 86400000))
    : '—';
  const buys = trades.filter(tr => (tr.side || '').toLowerCase() === 'buy').length;
  const sells = trades.length - buys;

  body.innerHTML = `
    <button class="tombstone-modal-close" aria-label="Close">×</button>
    <div class="tomb-modal-header">
      <div class="tomb-modal-cross">✝</div>
      <div class="tomb-modal-title">
        <div class="tomb-modal-rip">${t('tomb_rip') || 'In Loving Memory of'}</div>
        <h2 class="tomb-modal-id">${accountId} <span class="tomb-modal-strat">${tStrategy(meta.strategy_name || '', accountId)}</span></h2>
        <div class="tomb-modal-dates">${born} ✦ ${died} <span class="tomb-modal-days">(${lifeDays} ${t('tomb_days') || 'days'})</span></div>
      </div>
      <div class="tomb-modal-return ${retCls}">
        <div class="tomb-modal-return-label">${t('tomb_lifetime_return') || 'Lifetime Return'}</div>
        <div class="tomb-modal-return-value">${sign}${lifetimeRet.toFixed(2)}%</div>
      </div>
    </div>

    <div class="tomb-modal-eulogy">
      <div class="tomb-section-title">${t('tomb_eulogy') || '📜 Eulogy'}</div>
      <div class="tomb-eulogy-grid">
        <div><span class="tomb-k">${t('tomb_strategy') || 'Strategy'}:</span> <span>${meta.strategy_name || '—'}</span></div>
        <div><span class="tomb-k">${t('tomb_group') || 'Group'}:</span> <span>${meta.group || '—'}</span></div>
        <div><span class="tomb-k">${t('tomb_factors') || 'Factors'}:</span> <span>${meta.factors || '—'}</span></div>
        <div><span class="tomb-k">${t('tomb_initial') || 'Initial cash'}:</span> <span>${formatCurrency(initialCash)}</span></div>
        <div><span class="tomb-k">${t('tomb_final') || 'Final equity'}:</span> <span>${formatCurrency(finalEquity)}</span></div>
        <div><span class="tomb-k">${t('tomb_trades') || 'Total trades'}:</span> <span>${trades.length} (${buys} B / ${sells} S)</span></div>
      </div>
      ${meta.description ? `<div class="tomb-desc"><span class="tomb-k">${t('tomb_desc') || 'Description'}:</span> ${meta.description}</div>` : ''}
      <div class="tomb-cause-of-death">
        <span class="tomb-k">${t('tomb_cause') || '⚰️ Cause of retirement'}:</span>
        <span>${meta.retire_reason || '—'}</span>
      </div>
    </div>

    <div class="tomb-modal-section">
      <div class="tomb-section-title">${t('tomb_equity_lifetime') || '📈 Lifetime Equity Curve'}</div>
      <div id="tomb-equity-${accountId}" style="height:280px;position:relative;"></div>
    </div>

    <div class="tomb-modal-section">
      <div class="tomb-section-title">${t('tomb_factors_section') || '🧬 Factors / Strategy'}</div>
      <div id="tomb-factors-${accountId}" class="factors-container"></div>
    </div>

    <div class="tomb-modal-section">
      <div class="tomb-section-title">${t('tomb_final_positions') || '🪦 Final Positions (frozen)'}</div>
      <div id="tomb-pos-${accountId}">${createPositionsTable(positions, finalEquity)}</div>
    </div>

    <div class="tomb-modal-section">
      <div class="tomb-section-title">${t('tomb_all_trades') || `📜 All Trades (${trades.length})`}</div>
      <div id="tomb-trades-${accountId}">${createTradesTable(trades.slice().reverse())}</div>
    </div>
  `;
  body.querySelector('.tombstone-modal-close').addEventListener('click', () => {
    document.getElementById('tombstone-modal').classList.remove('open');
  });
  // Equity chart with markers — reuse renderRowEquity from components.js
  if (typeof renderRowEquity === 'function') {
    renderRowEquity(`tomb-equity-${accountId}`, equityCurve, accountId, accData.benchmarks, accData.alpha, trades, accData.snapshots || []);
  }
  // Factors
  const factorsContainer = document.getElementById(`tomb-factors-${accountId}`);
  if (factorsContainer) {
    if (factors.group === 'B') {
      const compId = `gp-comp-${Math.random().toString(36).slice(2,9)}`;
      renderGpBlock(factorsContainer, factors.factors || [], compId, factors.composite, accountId, factors.gp_info || '', factors.gp_params || []);
    } else {
      renderFactors(factorsContainer, factors.factors || [], factors.composite);
    }
  }
}

function paintAccounts(mode) {
  const grid = document.getElementById('accounts-grid');
  if (!grid || !_accountsCache) return;
  const sorted = sortAccounts(_accountsCache, mode);
  grid.innerHTML = '';
  sorted.forEach((acc, i) => {
    const card = createCard(acc);
    card.style.animationDelay = `${i * 0.02}s`;
    grid.appendChild(card);
  });
}
