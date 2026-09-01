// trade.js — main trading overview page

async function renderTradePage(routeToken) {
  const routeOk = () => typeof isRouteCurrent !== 'function' || isRouteCurrent(routeToken, '/trade');
  if (!routeOk()) return;
  renderHero({ distribution: {} });
  _equityAggregateCache = null; _equityFullCache = null; _equityFullPromise = null;

  loadTickerNames(state.market).catch(() => {});
  const summaryPromise = api('/trade/summary');
  const equityPromise = api('/trade/equity-curves?view=aggregate');
  const accountsPromise = api('/trade/accounts');

  const summaryTask = summaryPromise.then(summary => {
    if (!routeOk()) return;
    updateHeroSummary(summary);
    return loadSystemStatus(routeToken);
  }).catch(e => {
    if (e.name !== 'AbortError') console.warn('Failed to load trade summary', e);
    const banner = document.getElementById('system-status-banner');
    if (banner) { banner.className = 'system-status-banner system-status-degraded'; banner.textContent = t('system_unavailable'); }
  });
  const equityTask = equityPromise.then(eqData => {
    if (routeOk()) { _equityAggregateCache = eqData; renderEquityCurves(eqData); }
  }).catch(e => {
    if (e.name !== 'AbortError' && routeOk()) document.querySelector('.chart-section')?.remove();
  });
  const accountsTask = accountsPromise.then(accounts => {
    if (routeOk()) renderAccountCards(accounts);
  }).catch(e => { if (e.name !== 'AbortError') console.warn('Failed to load accounts', e); });

  await Promise.allSettled([summaryTask, equityTask, accountsTask]);
}

async function loadSystemStatus(routeToken) {
  const routeOk = () => typeof isRouteCurrent !== 'function' || isRouteCurrent(routeToken, '/trade');
  const s = await api('/system-status');
  if (!routeOk()) return;
  const host = document.getElementById('system-status-banner');
  if (!host) return;
  const q = s.quote_health || {};
  const v = s.valuation || {};
  const r = s.risk || {};
  const bad = (s.status || 'degraded') !== 'healthy';
  const isExtended = s.market_phase === 'extended';
  const pct = r.drawdown == null ? '—' : `${(Number(r.drawdown) * 100).toFixed(2)}%`;
  const coverage = `${v.complete_accounts || 0}/${v.active_accounts || 0}`;
  const inactive = (s.non_tradeable_accounts || []).map(x => x.account_id).join(', ');
  // Sparse extended-session prints are expected and are not actionable. The
  // backend still requires a fresh updater heartbeat and RTH quote coverage.
  const quoteStatus = ['closed', 'extended'].includes(q.status)
    ? ''
    : `<span>${t('system_quote')}: ${_escStatus(q.status || 'unknown')}</span>`;
  const sessionStatus = isExtended
    ? `<span>${t('system_extended_session')}</span>`
    : `<span>${t('system_valuation')}: ${coverage}</span>`;
  const valuationDetail = isExtended
    ? t('system_extended_valuation')
    : `${t('system_oldest_valuation')}: ${_escStatus(v.oldest_complete_at || '—')}`;
  host.className = `system-status-banner ${bad ? 'system-status-degraded' : 'system-status-healthy'}`;
  host.innerHTML = `
    <div class="system-status-head">
      <b>${bad ? t('system_degraded') : t('system_healthy')}</b>
      ${quoteStatus}
      ${sessionStatus}
      <span>${t('system_risk')}: ${_escStatus(r.state || 'UNKNOWN')} · DD ${pct}</span>
    </div>
    <div class="system-status-detail">
      ${valuationDetail}
      ${inactive ? ` · ${t('system_nontradeable')}: ${_escStatus(inactive)}` : ''}
    </div>`;
}

function _escStatus(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function updateHeroSummary(s) {
  const hero = document.querySelector('.hero');
  if (!hero) return;
  const d=s.distribution||{}, medCls=(d.median_pct||0)>=0?'positive':'negative', medSign=(d.median_pct||0)>=0?'+':'';
  const bestCls=d.best&&d.best.pnl_pct>=0?'positive':'negative', worstCls=d.worst&&d.worst.pnl_pct>=0?'positive':'negative';
  hero.innerHTML=`<div class="hero-label">${t('dist_title')}</div><div class="hero-value ${medCls}" id="hero-median">${medSign}${(d.median_pct||0).toFixed(2)}%</div>
    <div class="hero-pnl" style="font-size:1rem;opacity:0.85;">${t('dist_median')} across ${d.count||0} accounts${d.retired_count?` <span style="opacity:0.6;">(+${d.retired_count} ${t('retired_label')||'retired'})</span>`:''} &nbsp;·&nbsp; ${t('dist_win_rate')}: ${d.win_rate||0}% (${d.win_count||0}/${d.count||0})</div>
    <div class="stats-row"><div class="glass-card stat-box"><div class="stat-label">${t('dist_best')}</div><div class="stat-value ${bestCls}">${d.best?(d.best.pnl_pct>=0?'+':'')+formatPercent(d.best.pnl_pct):'—'}</div><div class="stat-label" style="font-size:0.7rem;opacity:0.7;">${d.best?d.best.account_id:''}</div></div>
    <div class="glass-card stat-box"><div class="stat-label">${t('dist_worst')}</div><div class="stat-value ${worstCls}">${d.worst?(d.worst.pnl_pct>=0?'+':'')+formatPercent(d.worst.pnl_pct):'—'}</div><div class="stat-label" style="font-size:0.7rem;opacity:0.7;">${d.worst?d.worst.account_id:''}</div></div>
    <div class="glass-card stat-box"><div class="stat-label">${t('dist_iqr')} (Q1–Q3)</div><div class="stat-value" style="font-size:1.1rem;">${formatPercent(d.q1_pct||0)} ~ ${formatPercent(d.q3_pct||0)}</div></div></div>`;
}

function renderHero(s) {
  const app = document.getElementById('app');
  const d = s.distribution || {};
  const medCls = (d.median_pct || 0) >= 0 ? 'positive' : 'negative';
  const medSign = (d.median_pct || 0) >= 0 ? '+' : '';
  const bestCls = d.best && d.best.pnl_pct >= 0 ? 'positive' : 'negative';
  const worstCls = d.worst && d.worst.pnl_pct >= 0 ? 'positive' : 'negative';
  const heroHtml = `
    <div id="system-status-banner" class="system-status-banner system-status-loading">${t('system_loading')}</div>
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
          <label class="sort-label" for="sort-select">${t('sort_by')}</label>
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
        <button class="account-tab active" data-tab="active" role="tab" aria-selected="true">${t('tab_active') || 'Active'} <span class="tab-count" id="tab-count-active">0</span></button>
        <button class="account-tab" data-tab="retired" role="tab" aria-selected="false">${t('tab_retired') || 'Retired'} <span class="tab-count" id="tab-count-retired">0</span></button>
      </div>
      <div class="accounts-grid" id="accounts-grid"></div>
      <div class="tombstone-wall" id="tombstone-wall" style="display:none;"></div>
    </div>
    ${typeof eventsSectionHtml === 'function' ? eventsSectionHtml() : ''}
  `;
  app.innerHTML = heroHtml;
  if (typeof startEventsStream === 'function') startEventsStream();
}

function installEquityTimeZoom(chart, container) {
  const scale = chart.timeScale();
  let fullRange = null;
  const rememberFullRange = () => {
    const range = scale.getVisibleLogicalRange();
    if (range && Number.isFinite(range.from) && Number.isFinite(range.to)) {
      fullRange = { from: range.from, to: range.to };
    }
  };
  requestAnimationFrame(rememberFullRange);

  container.addEventListener('wheel', event => {
    const range = scale.getVisibleLogicalRange();
    if (!range || !Number.isFinite(range.from) || !Number.isFinite(range.to)) return;
    event.preventDefault();
    const span = Math.max(0.5, range.to - range.from);
    const fullSpan = fullRange ? Math.max(0.5, fullRange.to - fullRange.from) : span;
    const nextSpan = Math.min(fullSpan, Math.max(0.5, span * (event.deltaY < 0 ? 0.78 : 1.28)));
    const rect = container.getBoundingClientRect();
    const anchor = Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(1, rect.width)));
    const anchorLogical = range.from + span * anchor;
    scale.setVisibleLogicalRange({
      from: anchorLogical - nextSpan * anchor,
      to: anchorLogical + nextSpan * (1 - anchor),
    });
  }, { passive: false });

  container.addEventListener('dblclick', () => {
    scale.fitContent();
    requestAnimationFrame(rememberFullRange);
  });
}

let _overviewEquityChart = null;
let _equityView = { mode: 'aggregate', showRetired: false };
let _equityAggregateCache = null, _equityFullCache = null, _equityFullPromise = null;

async function selectEquityView(options) {
  _equityView = { ..._equityView, ...options };
  const needsFull = _equityView.mode !== 'aggregate' || _equityView.showRetired;
  if (!needsFull) { renderEquityCurves(_equityAggregateCache); return; }
  if (!_equityFullCache) {
    _equityFullPromise ||= api('/trade/equity-curves?view=full');
    _equityFullCache = await _equityFullPromise;
  }
  renderEquityCurves(_equityFullCache);
}

function aggregateActiveCurves(data, options = { showRetired: false }) {
  const curves = data && data.curves ? data.curves : (data || {});
  const meta = data && data.meta ? data.meta : {};
  const entries = Array.isArray(curves) ? curves : Object.entries(curves).map(([name, points]) => ({ name, data: points }));
  const activeGroups = new Set(['A', 'B', 'F', 'Q']);
  const groupOf = name => {
    const clean = String(name || '').replace(/^C(?=[ABFQ]\d)/, '');
    return activeGroups.has(clean.charAt(0)) ? clean.charAt(0) : null;
  };
  const isBenchmark = name => /^(IDX|QQQ|SPY|CSI|沪深)/.test(String(name || ''));
  if (options.mode && options.mode !== 'aggregate') {
    const filtered = entries.filter(e => {
      if (isBenchmark(e.name)) return true;
      if (!options.showRetired && meta[e.name] && meta[e.name].status === 'retired') return false;
      return options.mode === 'all' || groupOf(e.name) === options.mode;
    });
    return { curves: filtered, meta };
  }
  const output = entries.filter(e => isBenchmark(e.name));
  for (const group of activeGroups) {
    const members = entries.filter(e => groupOf(e.name) === group && (options.showRetired || !meta[e.name] || meta[e.name].status !== 'retired'));
    if (!members.length) continue;
    const buckets = new Map();
    members.forEach(e => (e.data || []).forEach(p => {
      const ts = p.timestamp || p.time || p.date, value = Number(p.equity ?? p.value);
      if (!ts || !Number.isFinite(value)) return;
      if (!buckets.has(ts)) buckets.set(ts, []);
      buckets.get(ts).push(value);
    }));
    const points = [...buckets.entries()].sort((a,b) => String(a[0]).localeCompare(String(b[0]))).map(([timestamp, values]) => {
      values.sort((a,b) => a-b); const n=values.length, mid=Math.floor(n/2);
      return { timestamp, equity: n%2 ? values[mid] : (values[mid-1]+values[mid])/2 };
    });
    output.push({ name: `${group} · MEDIAN`, data: points, aggregate: true });
  }
  return { curves: output, meta };
}

function renderEquityCurves(data, options) {
  const container = document.getElementById('equity-chart');
  if (!container || !window.LightweightCharts) return;
  if (options) _equityView = { ..._equityView, ...options };
  const sourceData = data;
  data = aggregateActiveCurves(sourceData, _equityView);
  if (_overviewEquityChart) { try { _overviewEquityChart.remove(); } catch {} _overviewEquityChart = null; }
  container.textContent = '';
  let controls = document.getElementById('equity-curve-controls');
  if (!controls) {
    controls = document.createElement('div'); controls.id = 'equity-curve-controls'; controls.className = 'equity-curve-controls';
    container.parentElement.insertBefore(controls, container);
  }
  controls.innerHTML = `<label>${t('group') || '账户/组筛选'} <select id="equity-view-select"><option value="aggregate">${t('groups') || '活跃组中位数'}</option><option value="all">${t('all_accounts') || '全部账户'}</option>${['A','B','F','Q'].map(g=>`<option value="${g}">${g}</option>`).join('')}</select></label><label><input id="equity-retired-toggle" type="checkbox" ${_equityView.showRetired?'checked':''}> ${t('tab_retired') || '退休账户'}</label>`;
  controls.querySelector('#equity-view-select').value = _equityView.mode;
  controls.querySelector('#equity-view-select').addEventListener('change', e => selectEquityView({mode:e.target.value}).catch(console.warn));
  controls.querySelector('#equity-retired-toggle').addEventListener('change', e => selectEquityView({showRetired:e.target.checked}).catch(console.warn));

  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 420,
    layout: { background: { type: 'solid', color: 'transparent' }, textColor: 'rgba(0,0,0,0.65)', fontSize: 12 },
    grid: { vertLines: { color: 'rgba(0,0,0,0.06)' }, horzLines: { color: 'rgba(0,0,0,0.06)' } },
    crosshair: { mode: 0, vertLine: { color: 'rgba(0,0,0,0.15)', width: 1 }, horzLine: { color: 'rgba(0,0,0,0.15)', width: 1 } },
    rightPriceScale: { borderColor: 'rgba(0,0,0,0.12)' },
    timeScale: { borderColor: 'rgba(0,0,0,0.12)', timeVisible: true, secondsVisible: false, rightOffset: 6, barSpacing: 10, minBarSpacing: 0.5 },
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { axisPressedMouseMove: true, mouseWheel: false, pinch: true },
  });
  _overviewEquityChart = chart;
  registerRouteCleanup(() => { if (_overviewEquityChart === chart) { try { chart.remove(); } catch {} _overviewEquityChart = null; } });

  const seriesMap = {};
  const seriesNameByRef = new Map();
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
    seriesNameByRef.set(series, curve.name);
  });

  chart.timeScale().fitContent();
  installEquityTimeZoom(chart, container);

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
  let highlighted = null;
  const restoreHighlighted = () => {
    if (!highlighted) return;
    const previous = seriesMap[highlighted];
    if (previous && !previous.isBench) previous.series.applyOptions({ color: previous.color, lineWidth: previous.baseWidth, lineStyle: previous.baseStyle });
    highlighted = null;
  };
  chart.subscribeCrosshairMove(param => {
    if (!param.time || !param.seriesData || param.seriesData.size === 0) {
      tooltip.classList.remove('visible');
      restoreHighlighted();
      return;
    }
    // find hovered (highest value near mouse)
    let best = null, bestVal = -Infinity;
    param.seriesData.forEach((val, series) => {
      const v = val.value;
      if (v !== undefined) {
        const name = seriesNameByRef.get(series);
        if (name) { if (!best || Math.abs(v) > Math.abs(bestVal)) { best = name; bestVal = v; } }
      }
    });
    if (best) {
      tooltip.querySelector('.tooltip-name').textContent = best;
      tooltip.querySelector('.tooltip-value').textContent = formatCurrency(bestVal);
      tooltip.classList.add('visible');
      if (best !== highlighted) {
        restoreHighlighted();
        const current = seriesMap[best];
        if (current && !current.isBench) { current.series.applyOptions({ lineWidth: 2.5 }); highlighted = best; }
      }
    }
  });

  // resize
  const observer = new ResizeObserver(() => chart.applyOptions({ width: container.clientWidth }));
  observer.observe(container);
  registerRouteCleanup(() => observer.disconnect());
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
  const reason = _esc(a.retire_reason || t('retired_tooltip') || '');

  return `
    <button type="button" class="tombstone fade-in" data-id="${id}" title="${reason}" aria-label="${_esc(id)} ${_esc(t('retired_badge') || 'retired')}">
      <div class="tombstone-cross">✝</div>
      <div class="tombstone-rip">R.I.P.</div>
      <div class="tombstone-id">${_esc(id)}</div>

      <div class="tombstone-dates">${born} — ${died}</div>
      <div class="tombstone-return ${pnlCls}">${sign}${formatPercent(pnlPct)}</div>
      <div class="tombstone-epitaph">${_esc((a.retire_reason || '').slice(0, 60) || (t('retired_tooltip') || ''))}</div>
    </button>`;
}

let _tombstoneReturnFocus = null;
function closeTombstoneModal() {
  const modal = document.getElementById('tombstone-modal');
  if (modal) { modal.classList.remove('open'); if (modal._removeFocusTrap) { modal._removeFocusTrap(); modal._removeFocusTrap = null; } }
  if (_tombstoneReturnFocus && _tombstoneReturnFocus.isConnected) _tombstoneReturnFocus.focus();
}
async function openTombstoneModal(accountId) {
  _tombstoneReturnFocus = document.activeElement;
  let modal = document.getElementById('tombstone-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'tombstone-modal';
    modal.className = 'tombstone-modal';
    modal.innerHTML = `<div class="tombstone-modal-backdrop"></div><div class="tombstone-modal-body glass-card" role="dialog" aria-modal="true" aria-label="${t('retired_label') || 'Retired account details'}" tabindex="-1"></div>`;
    document.body.appendChild(modal);
    modal.querySelector('.tombstone-modal-backdrop').addEventListener('click', closeTombstoneModal);
    const onEscape = (e) => { if (e.key === 'Escape' && modal.classList.contains('open')) closeTombstoneModal(); };
    document.addEventListener('keydown', onEscape);
    registerRouteCleanup(() => { document.removeEventListener('keydown', onEscape); modal.remove(); });
  }
  const body = modal.querySelector('.tombstone-modal-body');
  body.innerHTML = `<div class="tombstone-modal-loading">${t('events_loading') || 'Loading…'}</div>`;
  modal.classList.add('open');
  modal._removeFocusTrap = trapModalFocus(body, closeTombstoneModal);
  requestAnimationFrame(() => (modal.querySelector('button') || body).focus());
  try {
    const [accData, factors] = await Promise.all([
      api(`/trade/account/${accountId}`),
      api(`/factors/${accountId}`).catch(() => ({ factors: [] })),
    ]);
    renderTombstoneModal(body, accountId, accData, factors);
  } catch (e) {
    body.innerHTML = `<button class="tombstone-modal-close" aria-label="Close">×</button><p style="color:var(--negative);padding:24px;">${t('load_failed')} ${e.message}</p>`;
    body.querySelector('.tombstone-modal-close').addEventListener('click', closeTombstoneModal);
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
  const tradeStats = accData.trade_stats || {};
  const tradeTotal = Number.isFinite(tradeStats.total) ? tradeStats.total : trades.length;
  const buys = Number.isFinite(tradeStats.buys) ? tradeStats.buys : trades.filter(tr => (tr.side || '').toLowerCase() === 'buy').length;
  const sells = Number.isFinite(tradeStats.sells) ? tradeStats.sells : trades.length - buys;

  body.innerHTML = `
    <button class="tombstone-modal-close" aria-label="Close">×</button>
    <div class="tomb-modal-header">
      <div class="tomb-modal-cross">✝</div>
      <div class="tomb-modal-title">
        <div class="tomb-modal-rip">${t('tomb_rip') || 'In Loving Memory of'}</div>
        <h2 class="tomb-modal-id">${_esc(accountId)}</h2>
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

        <div><span class="tomb-k">${t('tomb_group') || 'Group'}:</span> <span>${meta.group || '—'}</span></div>
        <div><span class="tomb-k">${t('tomb_factors') || 'Factors'}:</span> <span>${meta.factors || '—'}</span></div>
        <div><span class="tomb-k">${t('tomb_initial') || 'Initial cash'}:</span> <span>${formatCurrency(initialCash)}</span></div>
        <div><span class="tomb-k">${t('tomb_final') || 'Final equity'}:</span> <span>${formatCurrency(finalEquity)}</span></div>
        <div><span class="tomb-k">${t('tomb_trades') || 'Total trades'}:</span> <span>${tradeTotal} (${buys} B / ${sells} S)</span></div>
      </div>
      ${meta.description ? `<div class="tomb-desc"><span class="tomb-k">${t('tomb_desc') || 'Description'}:</span> ${_esc(meta.description)}</div>` : ''}
      <div class="tomb-cause-of-death">
        <span class="tomb-k">${t('tomb_cause') || '⚰️ Cause of retirement'}:</span>
        <span>${_esc(meta.retire_reason || '—')}</span>
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
      <div class="tomb-section-title">${t('tomb_trade_history') || '📜 Trade History'} (${tradeTotal})</div>
      <div id="tomb-trades-${accountId}">${createTradesTable(trades.slice().reverse())}</div>
      <div id="tomb-trades-status-${accountId}" class="tomb-trades-status">
        ${(t('tomb_showing_trades') || 'Showing {shown} of {total}').replace('{shown}', trades.length).replace('{total}', tradeTotal)}
      </div>
      ${accData.trades_next_cursor ? `<button type="button" id="tomb-load-trades-${accountId}" class="btn-secondary">${t('tomb_load_older_trades') || 'Load older trades'}</button>` : ''}
    </div>
  `;
  body.querySelector('.tombstone-modal-close').addEventListener('click', closeTombstoneModal);
  body.querySelector('.tombstone-modal-close').focus();
  // Equity chart with markers — reuse renderRowEquity from components.js
  if (typeof renderRowEquity === 'function') {
    renderRowEquity(`tomb-equity-${accountId}`, equityCurve, accountId, accData.benchmarks, accData.alpha, accData.trade_markers || trades, accData.snapshots || []);
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
    factorsContainer.insertAdjacentHTML('beforeend', createFactorAiBlock(accountId));
    loadFactorAi(accountId, accData, factors);
  }
  const loadTrades = document.getElementById(`tomb-load-trades-${accountId}`);
  if (loadTrades) {
    let cursor = accData.trades_next_cursor;
    const loaded = trades.slice().reverse();
    loadTrades.addEventListener('click', async () => {
      if (!cursor || loadTrades.disabled) return;
      loadTrades.disabled = true;
      try {
        const page = await api(`/trade/account/${encodeURIComponent(accountId)}/trades?limit=200&cursor=${encodeURIComponent(cursor)}`);
        loaded.push(...(page.trades || []));
        cursor = page.next_cursor;
        document.getElementById(`tomb-trades-${accountId}`).innerHTML = createTradesTable(loaded);
        const status = document.getElementById(`tomb-trades-status-${accountId}`);
        if (status) status.textContent = (t('tomb_showing_trades') || 'Showing {shown} of {total}')
          .replace('{shown}', loaded.length).replace('{total}', tradeTotal);
        if (!cursor) loadTrades.remove();
      } catch (error) {
        loadTrades.textContent = `${t('load_failed') || 'Load failed'} ${error.message}`;
      } finally {
        loadTrades.disabled = false;
      }
    });
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
