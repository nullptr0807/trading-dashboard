// components.js — reusable UI components

function createCard(account) {
  const id = account.id || account.account_id;
  const groupChar = id.charAt(0) === 'C' ? id.charAt(1) : id.charAt(0);
  const isA = groupChar === 'A';
  const isB = groupChar === 'B';
  const isQ = groupChar === 'Q';
  const isIDX = id.startsWith('IDX');
  const badgeClass = isA ? 'badge-a' : (isB ? 'badge-b' : (isQ ? 'badge-q' : 'badge-idx'));
  const isRetired = (account.status || 'active') === 'retired';
  const pnlPct = account.pnl_pct || 0;
  const pnlAbs = account.pnl || 0;
  const tradeCount = account.trade_count ?? 0;
  const sharpe = account.sharpe_ratio ?? 0;
  const sharpeClass = sharpe >= 0 ? 'positive' : 'negative';
  const pnlClass = pnlPct >= 0 ? 'positive' : 'negative';
  const pnlSign = pnlPct >= 0 ? '+' : '';
  const row = document.createElement('div');
  row.className = 'account-row fade-in' + (isRetired ? ' account-row-retired' : '');
  if (isRetired) row.style.opacity = '0.55';
  row.dataset.id = id;
  row.dataset.status = account.status || 'active';
  const rawName = account.strategy_name || account.strategy || '';
  // Strip verbose Chinese nicknames from B group; show a neutral tag instead
  const displayName = isB ? t('gp_evolved_factor') : tStrategy(rawName, id);
  const retiredPill = isRetired
    ? `<span class="retired-pill" title="${(account.retire_reason || t('retired_tooltip') || '').replace(/"/g,'&quot;')}">${t('retired_badge') || 'RETIRED'}${account.retired_at ? ' · ' + account.retired_at.slice(0,10) : ''}</span>`
    : '';
  row.innerHTML = `
    <div class="row-main">
      <div class="row-left">
        <span class="account-badge ${badgeClass}">${id}</span>
        <span class="row-strategy">${displayName}</span>
        ${retiredPill}
      </div>
      <div class="row-metrics">
        <div class="row-metric">
          <div class="row-metric-label">${t('card_equity')}</div>
          <div class="row-metric-value">${formatCurrency(account.equity)}</div>
        </div>
        <div class="row-metric">
          <div class="row-metric-label">${t('card_pnl')}</div>
          <div class="row-metric-value ${pnlClass}">${pnlSign}${formatCurrency(pnlAbs)}</div>
        </div>
        <div class="row-metric">
          <div class="row-metric-label">${t('card_return')}</div>
          <div class="row-metric-value ${pnlClass}">${pnlSign}${formatPercent(pnlPct)}</div>
        </div>
        <div class="row-metric row-metric-sub">
          <div class="row-metric-label">${t('card_trades')}</div>
          <div class="row-metric-value">${tradeCount}</div>
        </div>
        <div class="row-metric row-metric-sub">
          <div class="row-metric-label">${t('card_sharpe')}</div>
          <div class="row-metric-value ${sharpeClass}">${sharpe.toFixed(2)}</div>
        </div>
        <div class="row-chevron" aria-hidden="true">›</div>
      </div>
    </div>
    <div class="row-detail"></div>
  `;
  row.querySelector('.row-main').addEventListener('click', () => toggleRowExpand(row, id));
  return row;
}

function createSparkline(canvas, data, isBlue) {
  if (!data || !data.length) return;
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  canvas.style.width = rect.width + 'px';
  canvas.style.height = rect.height + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height;
  const min = Math.min(...data), max = Math.max(...data);
  const range = max - min || 1;
  const points = data.map((v, i) => [i / (data.length - 1) * w, h - ((v - min) / range) * h * 0.8 - h * 0.1]);
  const color = isBlue ? '#00aaff' : '#9b59f7';
  ctx.beginPath();
  ctx.moveTo(points[0][0], points[0][1]);
  for (let i = 1; i < points.length; i++) ctx.lineTo(points[i][0], points[i][1]);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';
  ctx.stroke();
  // gradient fill
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color.replace(')', ',0.15)').replace('rgb', 'rgba').replace('#', ''));
  // simpler approach
  ctx.lineTo(points[points.length - 1][0], h);
  ctx.lineTo(points[0][0], h);
  ctx.closePath();
  ctx.fillStyle = `${color}15`;
  ctx.fill();
}

function renderGpFactors(accountId, factors) {
  if (!factors || !factors.length) return '';
  return factors.map((f, i) => {
    const latexId = `gplatex-${accountId}-${i}-${Math.random().toString(36).slice(2,6)}`;
    const ic = f.ic != null ? (f.ic * 100).toFixed(2) + '%' : '—';
    const icClass = (f.ic || 0) >= 0 ? 'positive' : 'negative';
    const varsHtml = (f.vars_used || []).map(v =>
      `<li><code>${v.name}</code> — ${v.desc}</li>`).join('');
    const warnHtml = (f.warnings && f.warnings.length) ?
      `<div class="gp-factor-warn">⚠️ ${f.warnings.join('<br>⚠️ ')}</div>` : '';
    const alphaSrc = (f.alpha_source || '').replace(/\n/g, '<br>');
    return `
      <div class="gp-factor-card" data-latex="${encodeURIComponent(f.latex || '')}" data-id="${latexId}">
        <div class="gp-factor-head">
          <span class="gp-factor-name">${f.name || t('factor_n', {n: i+1})}</span>
          <span class="gp-factor-ic ${icClass}">IC ${ic}</span>
        </div>
        <div class="gp-factor-label">${t('factor_raw_s')}</div>
        <pre class="factor-gp">${f.s_expression || ''}</pre>
        <div class="gp-factor-label">${t('factor_math')}</div>
        <div class="gp-factor-latex" id="${latexId}"></div>
        ${f.intuition ? `<div class="gp-factor-section"><div class="gp-factor-label">${t('factor_intuition')}</div><div class="gp-factor-text">${f.intuition}</div></div>` : ''}
        ${f.motivation ? `<div class="gp-factor-section"><div class="gp-factor-label">${t('factor_motivation')}</div><div class="gp-factor-text">${f.motivation}</div></div>` : ''}
        ${alphaSrc ? `<div class="gp-factor-section"><div class="gp-factor-label">${t('factor_alpha')}</div><div class="gp-factor-text">${alphaSrc}</div></div>` : ''}
        ${varsHtml ? `<div class="gp-factor-label">${t('factor_vars')}</div><ul class="gp-factor-vars">${varsHtml}</ul>` : ''}
        ${warnHtml}
      </div>`;
  }).join('');
}

function renderGpBlock(container, factors, compositeId, composite, accountId, gpInfo, gpParams) {
  const gpFactorsHtml = renderGpFactors(accountId, factors);
  const paramsHtml = (gpParams && gpParams.length) ? `
    <div class="gp-params">
      ${gpParams.map(p => `
        <div class="gp-param">
          <div class="gp-param-head"><span class="gp-param-name">${p.name}</span><span class="gp-param-val">${p.value}</span></div>
          <div class="gp-param-detail">${p.detail}</div>
        </div>`).join('')}
    </div>` : '';
  const motivationHtml = composite ? `
    <div class="factor-composite">
      <div class="composite-title">${t('factor_composite')}</div>
      <div class="composite-formula" id="${compositeId}"></div>
      <div class="composite-motivation">${(composite.motivation || '').replace(/\n/g,'<br>')}</div>
    </div>` : '';
  container.innerHTML = `
    <div class="factor-item">
      <div class="factor-name" style="color:#b388ff;">${accountId} · ${t('gp_evolved_factor')}</div>
      <pre class="factor-gp">${gpInfo || t('factor_no_gp_params')}</pre>
      ${paramsHtml}
    </div>
    ${gpFactorsHtml}
    ${motivationHtml}`;
  // KaTeX for each gp factor
  container.querySelectorAll('.gp-factor-card').forEach(card => {
    const id = card.dataset.id;
    const latex = decodeURIComponent(card.dataset.latex || '');
    const el = document.getElementById(id);
    if (el && latex && window.katex) {
      try { katex.render(latex, el, { throwOnError: false, displayMode: true }); }
      catch(e) { el.textContent = latex; }
    } else if (el) el.textContent = latex;
  });
  // KaTeX for composite
  if (composite && composite.latex && window.katex) {
    const el = document.getElementById(compositeId);
    if (el) { try { katex.render(composite.latex, el, { throwOnError: false, displayMode: true }); } catch(e) { el.textContent = composite.latex; } }
  }
}

function renderFactors(container, factors, composite) {
  const hasFactors = factors && factors.length;
  const factorsHtml = hasFactors ? factors.map(f => `
    <div class="factor-item">
      <div class="factor-name">${f.name || ''}</div>
      <div class="factor-formula"></div>
      <div class="factor-block">
        <div class="factor-block-label">${t('factor_math_intuition')}</div>
        <div class="factor-block-text">${f.physics || f.explanation || f.description || ''}</div>
      </div>
      <div class="factor-block">
        <div class="factor-block-label">${t('factor_trade_motivation')}</div>
        <div class="factor-block-text">${f.motivation || ''}</div>
      </div>
    </div>
  `).join('') : `<p style="color:var(--text-secondary);font-size:13px;">${t('factor_no_data')}</p>`;

  const compHtml = composite ? `
    <div class="factor-composite">
      <div class="composite-title">${t('factor_composite')}</div>
      <div class="composite-formula"></div>
      <div class="composite-motivation">${(composite.motivation || '').replace(/\n/g, '<br>')}</div>
    </div>
  ` : '';

  container.innerHTML = factorsHtml + compHtml;

  const formulas = container.querySelectorAll('.factor-formula');
  (factors || []).forEach((f, i) => {
    if (formulas[i] && f.latex && window.katex) {
      try {
        katex.render(f.latex, formulas[i], { throwOnError: false, displayMode: true });
      } catch (e) { formulas[i].textContent = f.latex; }
    } else if (formulas[i] && f.formula) {
      formulas[i].textContent = f.formula;
    }
  });

  if (composite && composite.latex) {
    const el = container.querySelector('.composite-formula');
    if (el && window.katex) {
      try {
        katex.render(composite.latex, el, { throwOnError: false, displayMode: true });
      } catch (e) { el.textContent = composite.latex; }
    } else if (el) {
      el.textContent = composite.latex;
    }
  }
}

function createPositionsTable(positions, accountEquity) {
  if (!positions || !positions.length) return `<p style="color:var(--text-secondary);font-size:13px;">${t('no_positions')}</p>`;
  // Compute per-position market value; if accountEquity missing, fall back to sum of MVs
  const rows = positions.map(p => {
    const price = p.current_price ?? p.price ?? p.avg_cost ?? p.cost ?? 0;
    const shares = p.shares ?? p.qty ?? 0;
    const mv = price * shares;
    return { p, price, shares, mv };
  });
  const totalMv = rows.reduce((s, r) => s + r.mv, 0);
  const denom = (accountEquity && accountEquity > 0) ? accountEquity : totalMv;
  return `<table class="data-table">
    <thead><tr><th>${t('th_ticker')}</th><th>${t('th_side')}</th><th>${t('th_shares')}</th><th>${t('th_cost')}</th><th>${t('th_current_price')}</th><th>${t('th_market_value')}</th><th>${t('th_weight')}</th><th>${t('th_pnl')}</th></tr></thead>
    <tbody>${rows.map(({p, price, shares, mv}) => {
      const pnl = (price - (p.avg_cost ?? p.cost ?? 0)) * shares;
      const pnlClass = pnl >= 0 ? 'positive' : 'negative';
      const wgt = denom > 0 ? (mv / denom * 100) : 0;
      const wgtStr = wgt.toFixed(1) + '%';
      // color the weight bar subtly by proportion
      return `<tr>
        <td>${formatTicker(p.ticker || p.symbol)}</td>
        <td>${t('side_long')}</td>
        <td>${shares}</td>
        <td>${formatCurrency(p.avg_cost ?? p.cost ?? 0)}</td>
        <td>${formatCurrency(price)}</td>
        <td>${formatCurrency(mv)}</td>
        <td>
          <div class="weight-cell">
            <div class="weight-bar"><div class="weight-bar-fill" style="width:${Math.min(wgt, 100).toFixed(1)}%;"></div></div>
            <span class="weight-label">${wgtStr}</span>
          </div>
        </td>
        <td class="${pnlClass}">${formatCurrency(pnl)}</td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

function createTradesTable(trades) {
  if (!trades || !trades.length) return `<p style="color:var(--text-secondary);font-size:13px;">${t('no_trade_records')}</p>`;
  return `<table class="data-table">
    <thead><tr><th>${t('th_time')}</th><th>${t('th_ticker')}</th><th>${t('th_side')}</th><th>${t('th_shares')}</th><th>${t('th_price')}</th></tr></thead>
    <tbody>${trades.map(t => `<tr>
      <td>${formatDate(t.timestamp || t.time)}</td><td>${formatTicker(t.ticker || t.symbol)}</td><td>${t.side}</td>
      <td>${t.shares || t.qty}</td><td>${formatCurrency(t.price)}</td>
    </tr>`).join('')}</tbody></table>`;
}

function createSkeleton() {
  return `
    <div class="hero">
      <div class="skeleton skeleton-text" style="width:120px;margin:0 auto 8px;"></div>
      <div class="skeleton skeleton-title"></div>
      <div class="skeleton skeleton-text" style="width:160px;margin:8px auto;"></div>
    </div>
    <div class="section">
      <div class="skeleton skeleton-text" style="width:160px;margin-bottom:20px;"></div>
      <div class="skeleton" style="height:400px;"></div>
    </div>
    <div class="section">
      <div class="skeleton skeleton-text" style="width:160px;margin-bottom:20px;"></div>
      <div class="accounts-grid">
        ${Array(8).fill('<div class="skeleton skeleton-card"></div>').join('')}
      </div>
    </div>
  `;
}

async function toggleCardExpand(card, accountId) {
  // Legacy card click — redirect to row expand
  toggleRowExpand(card, accountId);
}

async function toggleRowExpand(row, accountId) {
  const detail = row.querySelector('.row-detail');
  if (!detail) return;
  const isOpen = row.classList.contains('expanded');
  // close siblings
  row.parentElement?.querySelectorAll('.account-row.expanded').forEach(r => {
    if (r !== row) { r.classList.remove('expanded'); const d = r.querySelector('.row-detail'); if (d) d.innerHTML = ''; }
  });
  if (isOpen) {
    row.classList.remove('expanded');
    setTimeout(() => { detail.innerHTML = ''; }, 350);
    return;
  }
  row.classList.add('expanded');
  detail.innerHTML = `
    <div class="row-detail-inner">
      <div class="row-detail-grid">
        <div class="row-detail-left">
          <div class="row-detail-section row-detail-equity">
            <div class="detail-section-title">${t('detail_equity')}</div>
            <div id="rowchart-${accountId}" style="height:220px;position:relative;"></div>
          </div>
          <div class="row-detail-section">
            <div class="detail-section-title">${t('detail_positions')}</div>
            <div id="rowpos-${accountId}"><div class="skeleton" style="height:60px;"></div></div>
          </div>
          <div class="row-detail-section">
            <div class="detail-section-title">${t('detail_recent_trades')}</div>
            <div id="rowtrades-${accountId}"><div class="skeleton" style="height:60px;"></div></div>
          </div>
        </div>
        <div class="row-detail-section row-detail-factors">
          <div class="detail-section-title">${t('detail_factors')}</div>
          <div id="rowfactors-${accountId}" class="factors-container"><div class="skeleton" style="height:80px;"></div></div>
        </div>
      </div>
    </div>
  `;
  try {
    const [accData, factors] = await Promise.all([
      api(`/trade/account/${accountId}`),
      api(`/factors/${accountId}`).catch(() => ({ factors: [] })),
    ]);
    const accEquity = (accData.equity_curve && accData.equity_curve.length) ? (accData.equity_curve[accData.equity_curve.length-1].equity) : (accData.state?.equity);
    detail.querySelector(`#rowpos-${accountId}`).innerHTML = createPositionsTable(accData.positions, accEquity);
    detail.querySelector(`#rowtrades-${accountId}`).innerHTML = createTradesTable((accData.trades || []).slice().reverse().slice(0, 50));
    const factorsContainer = detail.querySelector(`#rowfactors-${accountId}`);
    if (factors.group === 'B') {
      const compId = `gp-comp-${Math.random().toString(36).slice(2,9)}`;
      renderGpBlock(factorsContainer, factors.factors || [], compId, factors.composite, accountId, factors.gp_info || '', factors.gp_params || []);
    } else {
      renderFactors(factorsContainer, factors.factors || [], factors.composite);
    }
    renderRowEquity(`rowchart-${accountId}`, accData.equity_curve || accData.sparkline || [], accountId, accData.benchmarks, accData.alpha, accData.trades || [], accData.snapshots || []);
  } catch (e) {
    detail.querySelector('.row-detail-inner').innerHTML = `<p style="color:var(--negative);padding:16px;">${t('load_failed')} ${e.message}</p>`;
  }
}

function renderRowEquity(containerId, curve, accountId, benchmarks, alpha, trades, snapshots) {
  const container = document.getElementById(containerId);
  if (!container || !window.LightweightCharts || !curve || !curve.length) {
    if (container) container.innerHTML = `<p style="color:var(--text-secondary);font-size:13px;padding:12px;">${t('no_equity_data')}</p>`;
    return;
  }
  // Make container a positioned wrapper for the hover tooltip
  container.style.position = 'relative';
  const groupChar = accountId.charAt(0) === 'C' ? accountId.charAt(1) : accountId.charAt(0);
  const color = groupChar === 'A' ? '#00aaff' : (groupChar === 'B' ? '#b388ff' : (groupChar === 'Q' ? '#34d399' : '#ffb74d'));
  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth, height: 220,
    layout: { background: { type: 'solid', color: 'transparent' }, textColor: 'rgba(255,255,255,0.5)', fontSize: 11 },
    grid: { vertLines: { color: 'rgba(255,255,255,0.03)' }, horzLines: { color: 'rgba(255,255,255,0.03)' } },
    crosshair: { mode: 0 },
    timeScale: { borderColor: 'rgba(255,255,255,0.06)', timeVisible: true, secondsVisible: false },
    rightPriceScale: { borderColor: 'rgba(255,255,255,0.06)' },
  });
  const series = chart.addAreaSeries({ lineColor: color, topColor: color + '55', bottomColor: color + '05', lineWidth: 2 });

  const toSeries = (arr) => {
    if (!arr || !arr.length) return [];
    if (typeof arr[0] === 'number') {
      const now = Math.floor(Date.now() / 1000);
      return arr.map((v, i) => ({ time: now - (arr.length - 1 - i) * 3600, value: v }));
    }
    const byT = {};
    arr.forEach(p => {
      const t = Math.floor(new Date(p.timestamp || p.time).getTime() / 1000);
      if (!isNaN(t)) byT[t] = p.equity || p.value;
    });
    return Object.entries(byT).sort((a,b)=>a[0]-b[0]).map(([t,v])=>({time: Number(t), value: v}));
  };

  const data = toSeries(curve);
  if (data.length) series.setData(data);

  // Benchmarks
  const benchColors = { QQQ: '#ffb74d', SPY: '#81c784', '000300.SH': '#ff7043', '沪深300': '#ff7043' };
  (benchmarks || []).forEach(b => {
    const bData = toSeries(b.curve);
    if (!bData.length) return;
    const bs = chart.addLineSeries({
      color: benchColors[b.ticker] || '#888',
      lineWidth: 1.5,
      lineStyle: 2,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    bs.setData(bData);
  });

  // ---- Trade markers (B/S arrows) ----
  const dataTimes = data.map(d => d.time);
  const minT = dataTimes.length ? dataTimes[0] : 0;
  const maxT = dataTimes.length ? dataTimes[dataTimes.length-1] : 0;
  // Snap any timestamp to nearest data-point time so markers sit on the curve
  function snapTime(t) {
    if (!dataTimes.length) return t;
    if (t <= minT) return minT;
    if (t >= maxT) return maxT;
    let lo = 0, hi = dataTimes.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (dataTimes[mid] < t) lo = mid + 1; else hi = mid;
    }
    // lo is first index with dataTimes[lo] >= t; pick closer of lo-1 / lo
    const a = dataTimes[Math.max(0, lo - 1)], b = dataTimes[lo];
    return (Math.abs(a - t) <= Math.abs(b - t)) ? a : b;
  }

  const tradesByTime = {};
  (trades || []).forEach(tr => {
    if (!tr.timestamp) return;
    const raw = Math.floor(new Date(tr.timestamp).getTime() / 1000);
    if (isNaN(raw)) return;
    const tt = snapTime(raw);
    (tradesByTime[tt] = tradesByTime[tt] || []).push(tr);
  });
  const markerBuckets = {};
  Object.entries(tradesByTime).forEach(([tt, list]) => {
    const tnum = Number(tt);
    let buys = 0, sells = 0;
    list.forEach(x => { if ((x.side || '').toLowerCase() === 'buy') buys++; else sells++; });
    markerBuckets[tnum] = { buys, sells };
  });
  const markers = Object.entries(markerBuckets)
    .map(([tt, b]) => {
      const isBuy = b.buys >= b.sells;
      const n = b.buys + b.sells;
      return {
        time: Number(tt),
        position: isBuy ? 'belowBar' : 'aboveBar',
        color:    isBuy ? '#00ff88' : '#ff4466',
        shape:    isBuy ? 'arrowUp' : 'arrowDown',
        text: (isBuy ? 'B' : 'S') + (n > 1 ? '·' + n : ''),
      };
    })
    .sort((a, b) => a.time - b.time);
  if (markers.length && markers.length < 2000) series.setMarkers(markers);

  // ---- Hover tooltip (snapshot / trade) ----
  const tipEl = document.createElement('div');
  tipEl.className = 'row-chart-tip';
  tipEl.style.display = 'none';
  container.appendChild(tipEl);

  const snapByTs = {};
  const snapKeys = [];
  (snapshots || []).forEach(s => {
    const tt = Math.floor(new Date(s.timestamp).getTime() / 1000);
    if (!isNaN(tt)) { snapByTs[tt] = s; snapKeys.push(tt); }
  });
  snapKeys.sort((a, b) => a - b);

  function fmtMoney(v) {
    if (v == null || isNaN(v)) return '—';
    const sym = (typeof currencySymbol === 'function') ? currencySymbol() : '$';
    return sym + Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  function fmtTs(isoOrNum) {
    if (typeof isoOrNum === 'number') {
      const d = new Date(isoOrNum * 1000);
      return d.toLocaleString();
    }
    return isoOrNum;
  }

  function renderTradeTip(list) {
    const ts = list[0].timestamp || '';
    const rows = list.map(tr => {
      const side = (tr.side || '').toUpperCase();
      const sideC = side === 'BUY' ? 'positive' : 'negative';
      return `
        <div class="tip-trade">
          <div class="tip-trade-head">
            <span class="${sideC}" style="font-weight:600;">${side}</span>
            <span class="tip-ticker">${formatTicker(tr.ticker || '')}</span>
          </div>
          <div class="tip-trade-meta">${tr.shares} × ${fmtMoney(tr.price)}</div>
        </div>`;
    }).join('');
    tipEl.innerHTML = `<div class="tip-ts">${fmtTs(ts)}</div>${rows}`;
  }

  function renderSnapshotTip(snap) {
    const eq = snap.equity != null ? snap.equity : null;
    const cash = snap.cash != null ? snap.cash : null;
    const head = `
      <div class="tip-ts">${fmtTs(snap.timestamp)}</div>
      <div class="tip-summary">
        ${t('bt_equity_label')} ${fmtMoney(eq)}${cash != null ? ' · ' + t('bt_cash') + ' ' + fmtMoney(cash) : ''}
      </div>`;
    const holdings = snap.holdings || [];
    if (!holdings.length) {
      tipEl.innerHTML = head + `<div class="tip-empty">${t('bt_no_positions')}</div>`;
      return;
    }
    const shown = holdings.slice(0, 8);
    const hidden = holdings.length - shown.length;
    const body = shown.map(h => {
      const pnlC = (h.pnl_pct || 0) >= 0 ? 'positive' : 'negative';
      const sign = (h.pnl_pct || 0) >= 0 ? '+' : '';
      return `
        <div class="tip-hold">
          <span class="tip-ticker">${formatTicker(h.ticker)}</span>
          <span class="tip-hold-meta">${h.shares}×${fmtMoney(h.price)}</span>
          <span class="${pnlC} tip-hold-pnl">${sign}${(h.pnl_pct||0).toFixed(2)}%</span>
        </div>`;
    }).join('');
    const more = hidden > 0 ? `<div class="tip-empty">${t('bt_more_items', {n: hidden})}</div>` : '';
    tipEl.innerHTML = head + body + more;
  }

  chart.subscribeCrosshairMove(param => {
    if (!param || !param.time || !param.point) { tipEl.style.display = 'none'; return; }
    const tt = typeof param.time === 'number' ? param.time : null;
    if (tt === null) { tipEl.style.display = 'none'; return; }

    const tradeList = tradesByTime[tt];
    if (tradeList && tradeList.length) {
      renderTradeTip(tradeList);
    } else {
      let snap = snapByTs[tt];
      if (!snap && snapKeys.length) {
        let lo = 0, hi = snapKeys.length - 1, best = -1;
        while (lo <= hi) {
          const mid = (lo + hi) >> 1;
          if (snapKeys[mid] <= tt) { best = snapKeys[mid]; lo = mid + 1; } else hi = mid - 1;
        }
        if (best >= 0) snap = snapByTs[best];
      }
      if (!snap) {
        // No holdings snapshot yet — fall back to showing equity at hovered point
        const ptSeries = param.seriesData.get(series);
        const val = ptSeries ? (ptSeries.value ?? ptSeries.close) : null;
        tipEl.innerHTML = `<div class="tip-ts">${fmtTs(tt)}</div><div class="tip-summary">${t('bt_equity_label')} ${fmtMoney(val)}</div>`;
      } else {
        renderSnapshotTip(snap);
      }
    }

    tipEl.style.display = 'block';
    const wrapW = container.clientWidth;
    const wrapH = container.clientHeight;
    const tipW = tipEl.offsetWidth || 240;
    const tipH = tipEl.offsetHeight || 120;
    let left = param.point.x + 12;
    let top = param.point.y + 12;
    if (left + tipW > wrapW - 6) left = param.point.x - tipW - 12;
    if (top + tipH > wrapH - 6) top = Math.max(6, wrapH - tipH - 6);
    if (left < 6) left = 6;
    if (top < 6) top = 6;
    tipEl.style.left = left + 'px';
    tipEl.style.top = top + 'px';
  });

  chart.timeScale().fitContent();
  new ResizeObserver(() => chart.applyOptions({ width: container.clientWidth })).observe(container);

  // Alpha summary + legend
  if (alpha || benchmarks?.length) {
    const isCn = /^(CA|CB)\d+$/i.test(accountId) || accountId === 'IDX3';
    const legend = document.createElement('div');
    legend.className = 'alpha-legend';
    const strategyRet = alpha ? alpha.strategy_ret_pct : null;
    const sRetClass = strategyRet >= 0 ? 'positive' : 'negative';
    const sRetSign = strategyRet >= 0 ? '+' : '';
    const benchItems = (alpha?.benchmarks || []).map(b => {
      const alphaClass = b.alpha_pct >= 0 ? 'positive' : 'negative';
      const alphaSign = b.alpha_pct >= 0 ? '+' : '';
      const retClass = b.ret_pct >= 0 ? 'positive' : 'negative';
      const retSign = b.ret_pct >= 0 ? '+' : '';
      // CN benchmark = 沪深300 (orange-red); US has QQQ (orange) / SPY (green).
      let swatch;
      if (isCn) swatch = '#ff7043';
      else swatch = b.label.startsWith('QQQ') ? '#ffb74d' : '#81c784';
      return `
        <div class="alpha-row">
          <span class="alpha-swatch" style="background:${swatch};"></span>
          <span class="alpha-name">${b.label}</span>
          <span class="alpha-ret ${retClass}">${retSign}${b.ret_pct}%</span>
          <span class="alpha-sep">·</span>
          <span class="alpha-label">Alpha</span>
          <span class="alpha-val ${alphaClass}">${alphaSign}${b.alpha_pct}%</span>
        </div>`;
    }).join('');
    legend.innerHTML = `
      <div class="alpha-row alpha-strat">
        <span class="alpha-swatch" style="background:${color};"></span>
        <span class="alpha-name">${t('alpha_strategy')}</span>
        <span class="alpha-ret ${sRetClass}">${strategyRet != null ? (sRetSign + strategyRet + '%') : '—'}</span>
      </div>
      ${benchItems}
      <div class="alpha-hint">${t(isCn ? 'alpha_hint_cn' : 'alpha_hint')}</div>
    `;
    container.parentElement.appendChild(legend);
  }
}

async function openAccountDrawer(accountId) {
  let overlay = document.getElementById('account-drawer-overlay');
  if (overlay) overlay.remove();
  overlay = document.createElement('div');
  overlay.id = 'account-drawer-overlay';
  overlay.className = 'drawer-overlay';
  overlay.innerHTML = `
    <div class="drawer-panel glass-card">
      <div class="drawer-header">
        <div class="drawer-title">
          <span class="account-badge ${accountId.startsWith('A') ? 'badge-a' : 'badge-b'}">${accountId}</span>
          <span id="drawer-strategy" style="font-size:15px;color:var(--text-secondary);"></span>
        </div>
        <button class="drawer-close" aria-label="close">×</button>
      </div>
      <div class="drawer-body">
        <div class="drawer-grid">
          <div class="drawer-section">
            <div class="detail-section-title">${t('detail_equity')}</div>
            <div id="drawer-equity" style="height:300px;position:relative;"></div>
          </div>
          <div class="drawer-section">
            <div class="detail-section-title">${t('detail_factors')}</div>
            <div id="drawer-factors" class="factors-container"><div class="skeleton" style="height:120px;"></div></div>
          </div>
          <div class="drawer-section">
            <div class="detail-section-title">${t('detail_positions')}</div>
            <div id="drawer-positions"><div class="skeleton" style="height:80px;"></div></div>
          </div>
          <div class="drawer-section">
            <div class="detail-section-title">${t('detail_recent_trades')}</div>
            <div id="drawer-trades"><div class="skeleton" style="height:80px;"></div></div>
          </div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add('visible'));

  const close = () => { overlay.classList.remove('visible'); setTimeout(() => overlay.remove(), 300); };
  overlay.querySelector('.drawer-close').addEventListener('click', close);
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  document.addEventListener('keydown', function onEsc(e) { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onEsc); } });

  try {
    const [accData, factors] = await Promise.all([
      api(`/trade/account/${accountId}`),
      api(`/factors/${accountId}`)
    ]);
    overlay.querySelector('#drawer-strategy').textContent = tStrategy(factors.strategy_name || accData.strategy_name || '', accountId);
    const accEquity = (accData.equity_curve && accData.equity_curve.length) ? (accData.equity_curve[accData.equity_curve.length-1].equity) : (accData.state?.equity);
    overlay.querySelector('#drawer-positions').innerHTML = createPositionsTable(accData.positions, accEquity);
    overlay.querySelector('#drawer-trades').innerHTML = createTradesTable((accData.trades || []).slice().reverse().slice(0, 50));

    // Factors: handle A-group (list) and B-group (GP expressions)
    const factorsContainer = overlay.querySelector('#drawer-factors');
    if (factors.group === 'B') {
      const compId = `gp-comp-${Math.random().toString(36).slice(2,9)}`;
      renderGpBlock(factorsContainer, factors.factors || [], compId, factors.composite, accountId, factors.gp_info || '', factors.gp_params || []);
    } else {
      renderFactors(factorsContainer, factors.factors || [], factors.composite);
    }

    // Equity curve + benchmarks
    renderDrawerEquity(accData.equity_curve || accData.sparkline || [], accountId, accData.benchmarks || []);
  } catch (e) {
    overlay.querySelector('.drawer-body').innerHTML = `<p style="color:var(--negative);padding:20px;">${t('load_failed')} ${e.message}</p>`;
  }
}

function renderDrawerEquity(curve, accountId, benchmarks) {
  const container = document.getElementById('drawer-equity');
  if (!container || !window.LightweightCharts || !curve || !curve.length) {
    if (container) container.innerHTML = `<p style="color:var(--text-secondary);font-size:13px;padding:12px;">${t('no_equity_data')}</p>`;
    return;
  }
  const isA = accountId.startsWith('A');
  const color = isA ? '#00aaff' : '#b388ff';
  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth, height: 300,
    layout: { background: { type: 'solid', color: 'transparent' }, textColor: 'rgba(255,255,255,0.5)', fontSize: 11 },
    grid: { vertLines: { color: 'rgba(255,255,255,0.03)' }, horzLines: { color: 'rgba(255,255,255,0.03)' } },
    timeScale: { borderColor: 'rgba(255,255,255,0.06)', timeVisible: true, secondsVisible: false },
    rightPriceScale: { borderColor: 'rgba(255,255,255,0.06)' },
  });
  const series = chart.addAreaSeries({
    lineColor: color, topColor: color + '55', bottomColor: color + '05', lineWidth: 2,
  });
  // curve can be array of numbers (sparkline) or array of {timestamp, equity}
  let data;
  if (typeof curve[0] === 'number') {
    const now = Math.floor(Date.now() / 1000);
    data = curve.map((v, i) => ({ time: now - (curve.length - 1 - i) * 3600, value: v }));
  } else {
    const byT = {};
    curve.forEach(p => {
      const t = Math.floor(new Date(p.timestamp || p.time).getTime() / 1000);
      if (!isNaN(t)) byT[t] = p.equity || p.value;
    });
    data = Object.entries(byT).sort((a,b)=>a[0]-b[0]).map(([t,v])=>({time: Number(t), value: v}));
  }
  if (data.length) series.setData(data);

  // Render benchmark curves (QQQ/SPY or 沪深300) rebased to same initial capital
  const benchColors = { QQQ: '#ffb74d', SPY: '#81c784', IDX3: '#ffb74d', '沪深300': '#ffb74d', CSI300: '#ffb74d' };
  if (benchmarks && benchmarks.length) {
    benchmarks.forEach(b => {
      if (!b.curve || !b.curve.length) return;
      const bColor = benchColors[b.ticker] || benchColors[b.label] || '#888888';
      const bSeries = chart.addLineSeries({
        color: bColor, lineWidth: 2, lineStyle: 2, // dashed
        crosshairMarkerVisible: false,
        lastValueVisible: false, priceLineVisible: false,
      });
      const byT = {};
      b.curve.forEach(p => {
        const t = Math.floor(new Date(p.timestamp || p.time).getTime() / 1000);
        if (!isNaN(t)) byT[t] = p.equity || p.value;
      });
      const bData = Object.entries(byT).sort((a,b2)=>a[0]-b2[0]).map(([t,v])=>({time: Number(t), value: v}));
      if (bData.length) bSeries.setData(bData);
    });

    // Add legend
    const legend = document.createElement('div');
    legend.style.cssText = 'position:absolute;top:8px;right:12px;display:flex;gap:12px;font-size:11px;color:var(--text-secondary);z-index:2;';
    legend.innerHTML = `<span style="color:${color};">● ${accountId}</span>` +
      benchmarks.filter(b => b.curve && b.curve.length).map(b => {
        const bc = benchColors[b.ticker] || benchColors[b.label] || '#888';
        return `<span style="color:${bc};">┅ ${b.label || b.ticker}</span>`;
      }).join('');
    container.style.position = 'relative';
    container.appendChild(legend);
  }

  chart.timeScale().fitContent();
  new ResizeObserver(() => chart.applyOptions({ width: container.clientWidth })).observe(container);
}
