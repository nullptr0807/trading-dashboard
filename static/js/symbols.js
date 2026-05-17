// symbols.js — Traded-symbol aggregation tab
//
// Two views:
//   #/symbols          → searchable list of every ticker traded in the
//                         current market, with #accounts / #trades / realized PnL
//   #/symbols/<TICKER> → per-symbol drill-down: price curve overlaid with
//                         every account's buy/sell points + a per-account
//                         PnL table.

(function () {
  // Tiny debounced filter so big universes (1000+ tickers) stay smooth.
  const _state = { rows: [], filter: '', sortKey: 'last_trade_ts', sortDir: 'desc' };

  const SORT_KEYS = {
    accounts_count: 'num',
    trade_count: 'num',
    realized_pnl: 'num',
    last_trade_ts: 'ts',
  };

  function sortRows(rows) {
    const k = _state.sortKey;
    const dir = _state.sortDir === 'asc' ? 1 : -1;
    const kind = SORT_KEYS[k] || 'num';
    return rows.slice().sort((a, b) => {
      let av = a[k], bv = b[k];
      if (kind === 'ts') {
        av = av ? new Date(av).getTime() : 0;
        bv = bv ? new Date(bv).getTime() : 0;
      } else {
        av = Number(av) || 0;
        bv = Number(bv) || 0;
      }
      if (av < bv) return -1 * dir;
      if (av > bv) return  1 * dir;
      return 0;
    });
  }

  function setSort(key) {
    if (_state.sortKey === key) {
      _state.sortDir = _state.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      _state.sortKey = key;
      // sensible defaults: timestamps & pnl & counts → desc first
      _state.sortDir = 'desc';
    }
    paintSymbolTable();
  }

  async function renderSymbolsPage() {
    const app = document.getElementById('app');
    app.innerHTML = `
      <div class="symbols-page">
        <div class="page-hero">
          <h1 class="page-title">${t('sym_title')}</h1>
          <p class="page-subtitle">${t('sym_subtitle')}</p>
        </div>
        <div class="glass-card section symbols-card">
          <div class="symbols-toolbar">
            <input id="sym-search" class="sym-search-input"
                   placeholder="${t('sym_search_ph')}" autocomplete="off" />
            <span class="sym-count" id="sym-count"></span>
          </div>
          <div id="sym-table-host">
            <p style="color:var(--text-secondary);padding:20px;">${t('sym_loading')}</p>
          </div>
        </div>
      </div>
    `;

    // Pre-load CN ticker name map so search can match Chinese names too.
    if (state.market === 'CN') {
      try { await loadTickerNames('CN'); } catch (e) { /* noop */ }
    }

    let data;
    try {
      const res = await fetch(`/api/symbols?market=${state.market}`);
      data = await res.json();
    } catch (e) {
      document.getElementById('sym-table-host').innerHTML =
        `<p style="color:var(--negative);padding:20px;">Failed: ${e.message}</p>`;
      return;
    }

    _state.rows = data.symbols || [];
    _state.filter = '';
    paintSymbolTable();

    const input = document.getElementById('sym-search');
    let debTimer = null;
    input.addEventListener('input', () => {
      clearTimeout(debTimer);
      debTimer = setTimeout(() => {
        _state.filter = input.value.trim().toLowerCase();
        paintSymbolTable();
      }, 80);
    });
  }

  function paintSymbolTable() {
    const host = document.getElementById('sym-table-host');
    const cnNames = (typeof _tickerNameCache !== 'undefined' && _tickerNameCache.CN) || {};
    const f = _state.filter;
    let rows = _state.rows.filter(r => {
      if (!f) return true;
      const tk = (r.ticker || '').toLowerCase();
      const nm = (r.ticker_name_cn || cnNames[r.ticker]?.cn || '').toLowerCase();
      const en = (r.ticker_name_en || cnNames[r.ticker]?.en || '').toLowerCase();
      return tk.includes(f) || nm.includes(f) || en.includes(f);
    });

    const cnt = document.getElementById('sym-count');
    if (cnt) cnt.textContent = `${rows.length} / ${_state.rows.length}`;

    if (!rows.length) {
      host.innerHTML = `<p style="color:var(--text-secondary);padding:20px;">${t('sym_no_results')}</p>`;
      return;
    }

    rows = sortRows(rows);

    const cur = currencySymbol();
    const fmtTs = (s) => s ? s.slice(0, 10) : '—';

    const arrow = (key) => {
      if (_state.sortKey !== key) return '<span class="sym-sort-ind">↕</span>';
      return `<span class="sym-sort-ind active">${_state.sortDir === 'asc' ? '↑' : '↓'}</span>`;
    };
    const headerHtml = `
      <div class="sym-row sym-row-head">
        <div class="sym-cell-tk">${t('sym_col_ticker')}</div>
        <div class="sym-cell-num sym-sortable" data-sort="accounts_count">${t('sym_col_accounts')} ${arrow('accounts_count')}</div>
        <div class="sym-cell-num sym-sortable" data-sort="trade_count">${t('sym_col_trades')} ${arrow('trade_count')}</div>
        <div class="sym-cell-num sym-sortable" data-sort="realized_pnl">${t('sym_col_realized')} ${arrow('realized_pnl')}</div>
        <div class="sym-cell-num sym-sortable" data-sort="last_trade_ts">${t('sym_col_last')} ${arrow('last_trade_ts')}</div>
      </div>
    `;
    const bodyHtml = rows.map(r => {
      const cnMeta = cnNames[r.ticker] || {};
      const lang = (typeof getLang === 'function') ? getLang() : 'en';
      const nm = (lang === 'zh' ? (r.ticker_name_cn || cnMeta.cn) : (r.ticker_name_en || cnMeta.en)) || '';
      const tickerLabel = nm
        ? `<span class="sym-tk-code">${r.ticker}</span><span class="sym-tk-name">${nm}</span>`
        : `<span class="sym-tk-code">${r.ticker}</span>`;
      const pnlCls = r.realized_pnl > 0 ? 'positive' : (r.realized_pnl < 0 ? 'negative' : '');
      const pnlStr = (r.realized_pnl >= 0 ? '+' : '−') + cur + Math.abs(r.realized_pnl).toLocaleString('en-US',{minimumFractionDigits:2, maximumFractionDigits:2});
      return `
        <a class="sym-row sym-row-body" href="#/symbols/${encodeURIComponent(r.ticker)}">
          <div class="sym-cell-tk">${tickerLabel}</div>
          <div class="sym-cell-num">${r.accounts_count}</div>
          <div class="sym-cell-num">${r.trade_count}</div>
          <div class="sym-cell-num ${pnlCls}">${pnlStr}</div>
          <div class="sym-cell-num sym-cell-ts">${fmtTs(r.last_trade_ts)}</div>
        </a>
      `;
    }).join('');

    host.innerHTML = `<div class="sym-table">${headerHtml}${bodyHtml}</div>`;

    host.querySelectorAll('.sym-sortable').forEach(el => {
      el.addEventListener('click', () => setSort(el.dataset.sort));
    });
  }

  // ---------------------------------------------------------------- Detail
  async function renderSymbolDetail(ticker) {
    const app = document.getElementById('app');
    app.innerHTML = `
      <div class="symbols-page">
        <a class="sym-back" href="#/symbols">${t('sym_back')}</a>
        <div id="sym-detail-host">
          <p style="color:var(--text-secondary);padding:20px;">${t('sym_loading')}</p>
        </div>
      </div>
    `;

    if (state.market === 'CN') {
      try { await loadTickerNames('CN'); } catch (e) {}
    }

    let data;
    try {
      const res = await fetch(`/api/symbols/${encodeURIComponent(ticker)}?market=${state.market}`);
      if (!res.ok) {
        const txt = await res.text();
        document.getElementById('sym-detail-host').innerHTML =
          `<p style="color:var(--negative);padding:20px;">${res.status} — ${txt}</p>`;
        return;
      }
      data = await res.json();
    } catch (e) {
      document.getElementById('sym-detail-host').innerHTML =
        `<p style="color:var(--negative);padding:20px;">Failed: ${e.message}</p>`;
      return;
    }

    paintSymbolDetail(data);
  }

  function paintSymbolDetail(d) {
    const host = document.getElementById('sym-detail-host');
    const cur = currencySymbol();
    const lang = (typeof getLang === 'function') ? getLang() : 'en';
    const nm = (lang === 'zh' ? d.ticker_name_cn : d.ticker_name_en) || '';
    const titleStr = nm ? `${d.ticker} <span class="sym-detail-name">${nm}</span>` : d.ticker;

    const hero = `
      <div class="page-hero">
        <h1 class="page-title">${titleStr}</h1>
        <div class="sym-stat-row">
          <div class="sym-stat">
            <div class="sym-stat-label">${t('sym_detail_last_close')}</div>
            <div class="sym-stat-val">${cur}${(d.last_close || 0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</div>
          </div>
          <div class="sym-stat">
            <div class="sym-stat-label">${t('sym_detail_total_pnl')}</div>
            <div class="sym-stat-val ${d.total_pnl>=0?'positive':'negative'}">${signed(cur, d.total_pnl)}</div>
          </div>
          <div class="sym-stat">
            <div class="sym-stat-label">${t('sym_detail_realized')}</div>
            <div class="sym-stat-val ${d.total_realized_pnl>=0?'positive':'negative'}">${signed(cur, d.total_realized_pnl)}</div>
          </div>
          <div class="sym-stat">
            <div class="sym-stat-label">${t('sym_detail_unrealized')}</div>
            <div class="sym-stat-val ${d.total_unrealized_pnl>=0?'positive':'negative'}">${signed(cur, d.total_unrealized_pnl)}</div>
          </div>
          <div class="sym-stat">
            <div class="sym-stat-label">${t('sym_detail_n_accounts')}</div>
            <div class="sym-stat-val">${d.total_accounts}</div>
          </div>
        </div>
      </div>
    `;

    const chartCard = `
      <div class="sym-chart-grid">
        <div class="glass-card section sym-chart-col">
          <div class="section-title-row"><div class="section-title">${t('sym_chart_title')}</div></div>
          <div id="sym-chart" style="height:360px;position:relative;"></div>
          <div id="sym-chart-legend" class="sym-chart-legend"></div>
        </div>
        <div class="glass-card section sym-profile-col" id="sym-profile">
          <div class="section-title-row"><div class="section-title">${t('sym_profile_title')}</div></div>
          <div class="sym-profile-body"><p style="color:var(--text-secondary);">${t('sym_loading')}</p></div>
        </div>
      </div>
    `;

    const acctCard = `
      <div class="glass-card section">
        <div class="section-title-row"><div class="section-title">${t('sym_detail_n_accounts')}</div></div>
        <div class="sym-acct-table">
          <div class="sym-arow sym-arow-head">
            <div>${t('sym_acct_col_account')}</div>
            <div>${t('sym_acct_col_strategy')}</div>
            <div class="num">${t('sym_acct_col_trades')}</div>
            <div class="num">${t('sym_acct_col_realized')}</div>
            <div class="num">${t('sym_acct_col_unrealized')}</div>
            <div class="num">${t('sym_acct_col_total')}</div>
            <div class="num">${t('sym_acct_col_return')}</div>
            <div class="num">${t('sym_acct_col_holding')}</div>
          </div>
          ${d.accounts.map(a => `
            <div class="sym-arow sym-arow-body" data-account="${a.account}" data-trades='${encodeURIComponent(JSON.stringify(a.trades))}'>
              <div><span class="sym-acct-toggle" aria-hidden="true">▸</span><a href="#/trade?focus=${a.account}" class="sym-acct-link" onclick="event.stopPropagation();">${a.account}</a><span class="sym-grp grp-${a.group||'X'}">${a.group||''}</span></div>
              <div class="sym-strat">${a.strategy_name || '—'}</div>
              <div class="num">${a.trade_count}</div>
              <div class="num ${a.realized_pnl>=0?'positive':'negative'}">${signed(cur, a.realized_pnl)}</div>
              <div class="num ${a.unrealized_pnl>=0?'positive':'negative'}">${signed(cur, a.unrealized_pnl)}</div>
              <div class="num ${a.total_pnl>=0?'positive':'negative'}">${signed(cur, a.total_pnl)}</div>
              <div class="num ${a.return_pct>=0?'positive':'negative'}">${a.return_pct>=0?'+':''}${a.return_pct.toFixed(2)}%</div>
              <div class="num">${a.remaining_shares>0 ? a.remaining_shares + ' @ ' + cur + a.remaining_avg_cost.toFixed(2) : '—'}</div>
            </div>
          `).join('')}
        </div>
      </div>
    `;

    host.innerHTML = hero + chartCard + acctCard;
    bindAccountRowExpand(d.ticker);
    drawSymbolChart(d);
    loadProfile(d.ticker);
  }

  async function loadProfile(ticker) {
    const host = document.querySelector('#sym-profile .sym-profile-body');
    if (!host) return;
    try {
      const res = await fetch(`/api/symbols/${encodeURIComponent(ticker)}/profile?market=${state.market}`);
      const p = await res.json();
      const lang = (typeof getLang === 'function') ? getLang() : 'en';
      const pick = (zh, en) => (lang === 'zh' ? (zh || en) : en);
      const name = pick(p.name_zh, p.name);
      const summary = pick(p.summary_zh, p.summary);
      const sector = pick(p.sector_zh, p.sector);
      const industry = pick(p.industry_zh, p.industry);
      const parts = [];
      if (name) parts.push(`<div class="sym-prof-name">${escapeHtml(name)}</div>`);
      const meta = [];
      if (sector) meta.push(escapeHtml(sector));
      if (industry) meta.push(escapeHtml(industry));
      if (meta.length) parts.push(`<div class="sym-prof-meta">${meta.join(' · ')}</div>`);
      if (summary) parts.push(`<p class="sym-prof-summary">${escapeHtml(summary)}</p>`);
      const earn = p.next_earnings ? String(p.next_earnings).slice(0, 10) : null;
      parts.push(`<div class="sym-prof-earn"><span class="sym-prof-label">${t('sym_profile_next_earnings')}</span> <span class="sym-prof-val">${earn || '—'}</span></div>`);
      if (p.website) parts.push(`<div class="sym-prof-link"><a href="${escapeAttr(p.website)}" target="_blank" rel="noopener">${escapeHtml(p.website.replace(/^https?:\/\//,''))}</a></div>`);
      if (!parts.length) parts.push(`<p style="color:var(--text-secondary);">${t('sym_profile_unavailable')}</p>`);
      host.innerHTML = parts.join('');
    } catch (e) {
      host.innerHTML = `<p style="color:var(--text-secondary);">${t('sym_profile_unavailable')}</p>`;
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // Click an account row → expand FIFO trade-by-trade ledger inline.
  function bindAccountRowExpand(ticker) {
    document.querySelectorAll('.sym-arow-body').forEach(row => {
      row.addEventListener('click', (ev) => {
        // Don't expand if clicking the account link itself.
        if (ev.target.closest('a')) return;
        const acc = row.dataset.account;
        const next = row.nextElementSibling;
        if (next && next.classList.contains('sym-arow-detail')) {
          next.remove();
          row.classList.remove('expanded');
          return;
        }
        let trades = [];
        try { trades = JSON.parse(decodeURIComponent(row.dataset.trades || '[]')); }
        catch (e) { trades = []; }
        const det = document.createElement('div');
        det.className = 'sym-arow-detail';
        det.innerHTML = renderTradesLedger(trades, acc, ticker);
        row.after(det);
        row.classList.add('expanded');
      });
    });
  }

  // Render a per-trade ledger for one account on this ticker. FIFO walk so
  // each SELL row shows its realized PnL (matched against earliest open lots).
  function renderTradesLedger(trades, account, ticker) {
    const cur = currencySymbol();
    if (!trades || !trades.length) return '<div class="sym-ledger-empty">No trades.</div>';
    // sort by time ascending
    const tr = trades.slice().sort((a, b) =>
      new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());

    // FIFO state mirrors backend api/symbols.py
    const lots = [];  // [{shares, price}]
    let cumRealized = 0;
    const rows = tr.map(t => {
      const side = (t.side || '').toLowerCase();
      const sh = Number(t.shares) || 0;
      const px = Number(t.price) || 0;
      const fee = Number(t.cost) || 0;
      const notional = sh * px;
      let rowPnl = null;
      let matchedShares = 0;
      let matchedCost = 0;
      if (side === 'buy') {
        lots.push({ shares: sh, price: px });
        cumRealized -= fee;
      } else if (side === 'sell') {
        let remaining = sh;
        while (remaining > 1e-9 && lots.length) {
          const lot = lots[0];
          const take = Math.min(lot.shares, remaining);
          matchedCost += take * lot.price;
          matchedShares += take;
          rowPnl = (rowPnl || 0) + (px - lot.price) * take;
          lot.shares -= take;
          remaining -= take;
          if (lot.shares <= 1e-9) lots.shift();
        }
        cumRealized += (rowPnl || 0);
        cumRealized -= fee;
      }
      const remShares = lots.reduce((s, l) => s + l.shares, 0);
      const remAvg = remShares > 1e-9
        ? lots.reduce((s, l) => s + l.shares * l.price, 0) / remShares
        : 0;
      const ts = (t.timestamp || '').replace('T', ' ').slice(0, 19);
      const sideCls = side === 'buy' ? 'positive' : 'negative';
      const pnlCell = rowPnl != null
        ? `<span class="${rowPnl >= 0 ? 'positive' : 'negative'}">${signed(cur, rowPnl)}</span>`
        : '—';
      const matchAvg = matchedShares > 1e-9 ? matchedCost / matchedShares : 0;
      const matchInfo = side === 'sell' && matchedShares > 1e-9
        ? `<span class="sym-ledger-match">vs avg ${cur}${matchAvg.toFixed(2)}</span>`
        : '';
      return `
        <div class="sym-ledger-row">
          <div class="sym-ledger-ts">${ts}</div>
          <div class="sym-ledger-side ${sideCls}">${side.toUpperCase()}</div>
          <div class="num">${sh}</div>
          <div class="num">${cur}${px.toFixed(2)}</div>
          <div class="num">${cur}${notional.toFixed(2)}</div>
          <div class="num">${cur}${fee.toFixed(2)}</div>
          <div class="num">${pnlCell} ${matchInfo}</div>
          <div class="num sym-ledger-pos">${remShares > 0 ? remShares.toFixed(2) + ' @ ' + cur + remAvg.toFixed(2) : '—'}</div>
        </div>
      `;
    }).join('');

    return `
      <div class="sym-ledger-card">
        <div class="sym-ledger-head">
          <span><b>${account}</b> · ${ticker} · ${tr.length} trades</span>
          <span class="${cumRealized >= 0 ? 'positive' : 'negative'}">Realized ${signed(cur, cumRealized)}</span>
        </div>
        <div class="sym-ledger-table">
          <div class="sym-ledger-row sym-ledger-head-row">
            <div>Time</div>
            <div>Side</div>
            <div class="num">Shares</div>
            <div class="num">Price</div>
            <div class="num">Notional</div>
            <div class="num">Fee</div>
            <div class="num">Trade PnL</div>
            <div class="num">Position After</div>
          </div>
          ${rows}
        </div>
      </div>
    `;
  }

  function signed(cur, v) {
    const s = v >= 0 ? '+' : '−';
    return s + cur + Math.abs(v || 0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  }

  // Color per account — stable hash so same account always gets same hue.
  const ACCT_PALETTE = [
    '#ff7f50','#7b6cf6','#34d399','#f59e0b','#ec4899','#0ea5e9',
    '#a855f7','#22c55e','#ef4444','#14b8a6','#eab308','#6366f1',
    '#84cc16','#f43f5e','#06b6d4','#d946ef','#10b981','#fb7185',
  ];
  function colorForAccount(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return ACCT_PALETTE[h % ACCT_PALETTE.length];
  }

  function drawSymbolChart(d) {
    const host = document.getElementById('sym-chart');
    if (!host || !window.LightweightCharts) return;
    if (!d.price_curve || !d.price_curve.length) {
      host.innerHTML = `<p style="color:var(--text-secondary);padding:20px;">No price data.</p>`;
      return;
    }
    host.innerHTML = '';
    const chart = LightweightCharts.createChart(host, {
      width: host.clientWidth, height: 360,
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: 'rgba(0,0,0,0.65)', fontSize: 11 },
      grid: { vertLines: { color: 'rgba(0,0,0,0.06)' }, horzLines: { color: 'rgba(0,0,0,0.06)' } },
      crosshair: { mode: 0 },
      timeScale: { borderColor: 'rgba(0,0,0,0.12)', timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: 'rgba(0,0,0,0.12)' },
    });
    const priceSeries = chart.addAreaSeries({
      lineColor: '#0071e3', topColor: 'rgba(0,113,227,0.20)', bottomColor: 'rgba(0,113,227,0.02)', lineWidth: 2,
    });
    const data = d.price_curve.map(p => ({
      time: Math.floor(new Date(p.timestamp.replace(' ', 'T') + 'Z').getTime() / 1000),
      value: p.close,
    })).filter(x => !isNaN(x.time) && x.value != null).sort((a,b)=>a.time-b.time);
    priceSeries.setData(data);

    const dataTimes = data.map(x => x.time);
    const minT = dataTimes[0], maxT = dataTimes[dataTimes.length-1];
    function snap(tnum) {
      if (tnum <= minT) return minT;
      if (tnum >= maxT) return maxT;
      let lo = 0, hi = dataTimes.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (dataTimes[mid] < tnum) lo = mid + 1; else hi = mid;
      }
      const a = dataTimes[Math.max(0, lo - 1)], b = dataTimes[lo];
      return Math.abs(a - tnum) <= Math.abs(b - tnum) ? a : b;
    }

    // Group all trades by account, project markers onto the price line.
    const markers = [];
    const legendItems = [];
    d.accounts.forEach(a => {
      const col = colorForAccount(a.account);
      legendItems.push({ account: a.account, color: col, total: a.total_pnl });
      a.trades.forEach(tr => {
        const raw = Math.floor(new Date(tr.timestamp).getTime() / 1000);
        if (isNaN(raw)) return;
        const tt = snap(raw);
        const isBuy = (tr.side || '').toLowerCase() === 'buy';
        markers.push({
          time: tt,
          position: isBuy ? 'belowBar' : 'aboveBar',
          color: col,
          shape: isBuy ? 'arrowUp' : 'arrowDown',
          text: a.account + (isBuy ? ' B' : ' S'),
          _meta: { account: a.account, side: tr.side, shares: tr.shares, price: tr.price, ts: tr.timestamp },
        });
      });
    });
    markers.sort((a, b) => a.time - b.time);
    if (markers.length && markers.length < 3000) priceSeries.setMarkers(markers);

    // Legend
    const cur = currencySymbol();
    const legend = document.getElementById('sym-chart-legend');
    if (legend) {
      legend.innerHTML = legendItems
        .sort((a, b) => b.total - a.total)
        .map(it => `<span class="sym-legend-chip" style="--c:${it.color};">
            <span class="dot"></span>${it.account}
            <span class="${it.total>=0?'positive':'negative'}">${signed(cur, it.total)}</span>
          </span>`).join('');
    }

    // Hover tooltip — show price on date + any trade markers there
    const tip = document.createElement('div');
    tip.className = 'sym-chart-tip';
    tip.style.display = 'none';
    host.appendChild(tip);

    const markersByTime = {};
    markers.forEach(m => { (markersByTime[m.time] = markersByTime[m.time] || []).push(m); });

    chart.subscribeCrosshairMove(param => {
      if (!param || !param.time || !param.point) { tip.style.display = 'none'; return; }
      const px = priceSeries.dataByIndex
        ? null
        : null;
      const v = param.seriesData ? param.seriesData.get(priceSeries) : null;
      const close = v && (v.value || v.close);
      const dStr = new Date(param.time * 1000).toISOString().slice(0,10);
      const trList = markersByTime[param.time] || [];
      let html = `<div class="tip-head">${dStr}</div>`;
      if (close != null) html += `<div>${cur}${Number(close).toFixed(2)}</div>`;
      if (trList.length) {
        html += '<div class="tip-sep"></div>';
        trList.forEach(m => {
          const meta = m._meta;
          const sCls = (meta.side||'').toLowerCase()==='buy' ? 'positive' : 'negative';
          html += `<div class="tip-tr"><span style="color:${m.color}">●</span> <b>${meta.account}</b> <span class="${sCls}">${meta.side.toUpperCase()}</span> ${meta.shares}@${cur}${Number(meta.price).toFixed(2)}</div>`;
        });
      }
      tip.innerHTML = html;
      tip.style.display = 'block';
      const rect = host.getBoundingClientRect();
      const x = Math.min(param.point.x + 14, rect.width - tip.offsetWidth - 8);
      const y = Math.max(8, param.point.y - tip.offsetHeight - 12);
      tip.style.left = x + 'px';
      tip.style.top = y + 'px';
    });

    // Resize
    const ro = new ResizeObserver(() => chart.applyOptions({ width: host.clientWidth }));
    ro.observe(host);
  }

  // expose
  window.renderSymbolsPage = renderSymbolsPage;
  window.renderSymbolDetail = renderSymbolDetail;
})();
