// backtest.js — backtest analysis page

async function renderBacktestPage() {
  const app = document.getElementById('app');
  app.innerHTML = `
    <div class="backtest-layout fade-in">
      <div class="config-panel glass-card">
        <div id="bt-qlib-status"></div>
        <div class="section">
          <div class="section-title">${t('bt_account_selection')}</div>
          <div id="bt-accounts-loading" style="color:var(--text-secondary);font-size:13px;">${t('bt_loading')}</div>
          <div id="bt-accounts"></div>
        </div>
        <div class="section">
          <div class="section-title">${t('bt_params')}</div>
          <div class="bt-field">
            <label>${t('bt_initial_capital')}</label>
            <input type="number" id="bt-capital" class="bt-input" value="${state.market === 'CN' ? 100000 : 10000}">
          </div>
          <div class="bt-field">
            <label>${t('bt_start_date')}</label>
            <input type="date" id="bt-start" class="bt-input">
          </div>
          <div class="bt-field">
            <label>${t('bt_end_date')}</label>
            <input type="date" id="bt-end" class="bt-input">
          </div>
        </div>
        <button class="btn btn-accent bt-run-btn" id="bt-run">${t('bt_run')}</button>
      </div>
      <div class="results-panel">
        <div id="bt-results-placeholder" style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-secondary);font-size:15px;">
          ${t('bt_placeholder')}
        </div>
        <div id="bt-results" style="display:none;">
          <div class="section">
            <div class="section-title">${t('bt_summary_stats')}</div>
            <div id="bt-data-stats" style="font-size:11px;color:var(--text-secondary);font-family:monospace;margin-bottom:10px;"></div>
            <div class="bt-stats-grid" id="bt-stats-grid"></div>
          </div>
          <div class="section">
            <div class="section-title">${t('bt_equity_dashed')}</div>
            <div class="glass-card" id="bt-chart-container" style="height:400px;padding:0;overflow:hidden;border-radius:var(--radius-sm);"></div>
          </div>
          <div class="section">
            <div class="section-title">${t('bt_account_comparison')}</div>
            <div style="overflow-x:auto;">
              <table class="data-table" id="bt-comparison-table">
                <thead><tr>
                  <th>${t('th_account')}</th><th>${t('th_strategy')}</th><th>${t('th_total_return')}</th><th>${t('th_max_dd')}</th>
                  <th>Sharpe</th><th>Sortino</th><th>${t('th_win_rate')}</th><th>${t('th_profit_factor')}</th><th>${t('th_total_trades')}</th>
                </tr></thead>
                <tbody id="bt-comparison-body"></tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;

  // Load accounts and date range
  try {
    const [accountsRes, dateRange, qlibStatus] = await Promise.all([
      api('/backtest/accounts'),
      api('/backtest/date-range'),
      api('/backtest/qlib-status').catch(() => null),
    ]);
    if (qlibStatus) renderQlibStatusBanner(qlibStatus);
    renderAccountSelector(accountsRes.accounts || []);
    if (dateRange.min_date) document.getElementById('bt-start').value = dateRange.min_date.slice(0, 10);
    if (dateRange.max_date) document.getElementById('bt-end').value = dateRange.max_date.slice(0, 10);
    // Show data availability hint under date inputs
    const startInput = document.getElementById('bt-start');
    const hint = document.createElement('div');
    hint.style.cssText = 'font-size:11px;color:var(--text-secondary);margin-top:4px;';
    hint.textContent = t('bt_date_hint');
    startInput.parentElement.appendChild(hint);
  } catch (e) {
    document.getElementById('bt-accounts-loading').textContent = t('bt_load_failed') + ' ' + e.message;
  }

  document.getElementById('bt-run').addEventListener('click', runBacktest);
}

function renderQlibStatusBanner(s) {
  const host = document.getElementById('bt-qlib-status');
  if (!host || !s) return;
  const cov = s.coverage || {};
  const models = cov.models || {};
  const nWith = s.models_with_checkpoints || 0;
  const nTotal = s.models_total || 10;
  const totalMb = ((cov.total_bytes || 0) / 1048576).toFixed(2);
  const earliest = s.earliest_full_replay_date || '—';

  // Per-model coverage table (compact)
  const modelOrder = ['Q01','Q02','Q03','Q04','Q05','Q06','Q07','Q08','Q09','Q10'];
  const covRowsHtml = modelOrder.map(mid => {
    const m = models[mid];
    if (!m) return `<tr><td>${mid}</td><td colspan="3" class="qst-empty">—</td></tr>`;
    return `<tr>
      <td>${mid}</td>
      <td>${m.first || '—'} → ${m.last || '—'}</td>
      <td class="qst-num">${m.count}</td>
      <td class="qst-num">${(m.bytes/1024).toFixed(0)} KB</td>
    </tr>`;
  }).join('');

  const leakHtml = (s.leakage_vectors || []).map(v => `
    <li><b>${v.title}</b><br><span class="qst-desc">${v.desc}</span></li>
  `).join('');

  const todoHtml = (s.todo || []).map(it => `
    <li class="${it.done ? 'qst-done' : 'qst-pending'}">
      <span class="qst-check">${it.done ? '✅' : '⬜'}</span> ${it.text}
    </li>
  `).join('');

  const doneHtml = (s.done || []).map(d => `<li>✓ ${d}</li>`).join('');

  host.innerHTML = `
    <div class="qst-banner">
      <div class="qst-header" id="qst-toggle">
        <span class="qst-pill">⚠️ ${t('bt_qst_pill') || 'Q 组回测暂未启用'}</span>
        <span class="qst-summary">
          ${t('bt_qst_coverage') || 'Checkpoint 覆盖'}: <b>${nWith}/${nTotal}</b> ${t('bt_qst_models') || '模型'} ·
          ${t('bt_qst_earliest') || '可回放起点'}: <b>${earliest}</b> ·
          ${t('bt_qst_disk') || '磁盘'}: ${totalMb} MB
        </span>
        <span class="qst-arrow">▾</span>
      </div>
      <div class="qst-body" id="qst-body">
        <div class="qst-intro">
          ${t('bt_qst_intro') || 'Qlib 模型回测涉及前瞻偏差风险。当前不允许在历史数据上 retrain — 我们正在用 frozen daily checkpoint 方案解决。'}
        </div>

        <div class="qst-grid">
          <div class="qst-section">
            <div class="qst-section-title">🚨 ${t('bt_qst_leakage') || '风险向量'}</div>
            <ul class="qst-list">${leakHtml}</ul>
          </div>

          <div class="qst-section">
            <div class="qst-section-title">📋 ${t('bt_qst_plan') || '解决方案'}</div>
            <div class="qst-plan-text">${(s.plan && s.plan.approach) || ''}</div>
            <div class="qst-plan-meta">
              ${t('bt_qst_size_per_day') || '单日大小'}: ~${(s.plan && s.plan.checkpoint_size_per_day_kb) || 0} KB ·
              ${t('bt_qst_yearly') || '年存储'}: ~${(s.plan && s.plan.yearly_storage_mb) || 0} MB
            </div>
          </div>
        </div>

        <div class="qst-section">
          <div class="qst-section-title">🗂️ ${t('bt_qst_todo') || 'TODO / 已完成'}</div>
          <ul class="qst-todo">${todoHtml}</ul>
        </div>

        <div class="qst-section">
          <div class="qst-section-title">📊 ${t('bt_qst_coverage_table') || '每模型 Checkpoint 覆盖'} (${s.market})</div>
          <table class="qst-table">
            <thead><tr>
              <th>${t('bt_qst_model') || '模型'}</th>
              <th>${t('bt_qst_range') || '日期范围'}</th>
              <th>${t('bt_qst_count') || '天数'}</th>
              <th>${t('bt_qst_size') || '大小'}</th>
            </tr></thead>
            <tbody>${covRowsHtml}</tbody>
          </table>
          <div class="qst-coverage-note">
            ${nWith === 0
              ? (t('bt_qst_no_data') || 'Checkpoint 累积尚未开始。每日 23:00 UTC cron 训练后会自动写入；首条数据将在下次 cron 运行后出现。')
              : (t('bt_qst_data_growing') || `已积累 ${nWith}/${nTotal} 个模型，每日 23:00 UTC 自动追加。`)}
          </div>
        </div>

        ${doneHtml ? `<div class="qst-section">
          <div class="qst-section-title">✅ ${t('bt_qst_done') || '已上线'}</div>
          <ul class="qst-done-list">${doneHtml}</ul>
        </div>` : ''}
      </div>
    </div>
  `;

  // Collapsible
  const toggle = document.getElementById('qst-toggle');
  const body = document.getElementById('qst-body');
  const arrow = host.querySelector('.qst-arrow');
  // Default: collapsed (one-time read), but expand on first visit
  const seen = localStorage.getItem('qst_collapsed') === '1';
  if (seen) {
    body.style.display = 'none';
    if (arrow) arrow.textContent = '▸';
  }
  toggle.addEventListener('click', () => {
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
    if (arrow) arrow.textContent = open ? '▸' : '▾';
    localStorage.setItem('qst_collapsed', open ? '1' : '0');
  });
}

function renderAccountSelector(accounts) {
  const container = document.getElementById('bt-accounts');
  const loading = document.getElementById('bt-accounts-loading');
  loading.style.display = 'none';

  const groups = {};
  accounts.forEach(a => {
    const g = a.group || t('bt_ungrouped');
    if (!groups[g]) groups[g] = [];
    groups[g].push(a);
  });

  let html = '';
  for (const [group, accs] of Object.entries(groups)) {
    html += `<div class="bt-group">
      <label class="bt-checkbox bt-group-header">
        <input type="checkbox" data-group="${group}" class="bt-group-toggle">
        <span>${group}</span>
      </label>
      <div class="bt-group-items">`;
    accs.forEach(a => {
      const aid = a.account || a.account_id;
      const isRetired = (a.status || 'active') === 'retired';
      // Q-accounts (Q01-Q10 / CQ01-CQ10): backtest currently disabled
      // (Qlib look-ahead protection — see status banner above).
      const isQlib = /^(C?Q)\d+$/.test(aid);
      const retiredBadge = isRetired
        ? ` <span class="retired-pill" title="${a.retire_reason || ''}">${t('retired_badge') || 'RETIRED'}${a.retired_at ? ' · ' + (a.retired_at.slice(0,10)) : ''}</span>`
        : '';
      const qlibBadge = isQlib
        ? ` <span class="qlib-block-pill" title="${(t('bt_qlib_block_tooltip') || 'Qlib 模型回测暂未支持，详见上方说明').replace(/"/g,'&quot;')}">${t('bt_qlib_blocked_badge') || 'CHECKPOINT 累积中'}</span>`
        : '';
      const cls = `bt-checkbox${isRetired ? ' bt-account-retired' : ''}${isQlib ? ' bt-account-qlib-blocked' : ''}`;
      const disabledAttr = isQlib ? 'disabled' : '';
      html += `<label class="${cls}">
        <input type="checkbox" value="${aid}" data-group="${group}" class="bt-account-cb" ${disabledAttr}>
        <span>${aid} — ${tStrategy(a.strategy_name || '', aid)}${retiredBadge}${qlibBadge}</span>
      </label>`;
    });
    html += `</div></div>`;
  }
  container.innerHTML = html;

  // Group toggle logic
  container.querySelectorAll('.bt-group-toggle').forEach(toggle => {
    toggle.addEventListener('change', () => {
      const group = toggle.dataset.group;
      const checked = toggle.checked;
      container.querySelectorAll(`.bt-account-cb[data-group="${group}"]`).forEach(cb => {
        if (cb.disabled) return;  // skip Q (Qlib) accounts that are blocked
        cb.checked = checked;
      });
    });
  });
}

async function runBacktest() {
  const btn = document.getElementById('bt-run');
  const selected = Array.from(document.querySelectorAll('.bt-account-cb:checked')).map(cb => cb.value);
  if (selected.length === 0) { alert(t('bt_pick_one_account')); return; }

  const body = {
    accounts: selected,
    initial_capital: parseFloat(document.getElementById('bt-capital').value) || (state.market === 'CN' ? 100000 : 10000),
    start_date: document.getElementById('bt-start').value,
    end_date: document.getElementById('bt-end').value,
    market: state.market,
  };
  if (!body.start_date || !body.end_date) { alert(t('bt_pick_dates')); return; }

  btn.disabled = true;
  btn.textContent = t('bt_running');

  // Show progress UI
  const placeholder = document.getElementById('bt-results-placeholder');
  const results = document.getElementById('bt-results');
  results.style.display = 'none';
  placeholder.style.display = 'flex';
  placeholder.innerHTML = `
    <div style="width:100%;max-width:520px;padding:24px;">
      <div style="font-size:15px;margin-bottom:14px;color:var(--text-primary);">${t('bt_running_title')}</div>
      <div id="bt-progress-msg" style="font-size:12px;color:var(--text-secondary);margin-bottom:10px;font-family:monospace;">${t('bt_starting')}</div>
      <div style="background:rgba(0,0,0,0.04);height:10px;border-radius:5px;overflow:hidden;">
        <div id="bt-progress-bar" style="width:0%;height:100%;background:linear-gradient(90deg,var(--accent-blue),var(--accent-purple));transition:width 0.3s;"></div>
      </div>
      <div id="bt-progress-pct" style="font-size:11px;color:var(--text-secondary);margin-top:8px;text-align:right;">0%</div>
    </div>
  `;

  try {
    const startRes = await fetch('/api/backtest/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!startRes.ok) throw new Error(t('bt_start_failed') + ' ' + startRes.status);
    const { job_id } = await startRes.json();

    // Poll every 500ms
    const pollInterval = 500;
    while (true) {
      await new Promise(r => setTimeout(r, pollInterval));
      const r = await fetch(`/api/backtest/job/${job_id}`);
      if (!r.ok) throw new Error(t('bt_poll_failed'));
      const j = await r.json();
      const bar = document.getElementById('bt-progress-bar');
      const pct = document.getElementById('bt-progress-pct');
      const msg = document.getElementById('bt-progress-msg');
      if (bar) bar.style.width = (j.progress || 0) + '%';
      if (pct) pct.textContent = (j.progress || 0).toFixed(1) + '%';
      if (msg) msg.textContent = j.message || '...';
      if (j.status === 'done') { renderBacktestResults(j.result); break; }
      if (j.status === 'error') throw new Error(j.error || t('bt_generic_fail'));
    }
  } catch (e) {
    placeholder.innerHTML = `<div style="color:#ff6666;padding:24px;">${t('bt_error_prefix')} ${e.message}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = t('bt_run');
  }
}

function renderBacktestResults(data) {
  document.getElementById('bt-results-placeholder').style.display = 'none';
  document.getElementById('bt-results').style.display = 'block';

  // Stats grid (combined)
  const s = data.combined?.stats || {};
  const meta = data.meta || {};
  const ds = meta.data_stats || {};
  const bs = meta.benchmark_stats || {};
  const dataStatsEl = document.getElementById('bt-data-stats');
  if (dataStatsEl) {
    const parts = [];
    if (ds.interval) {
      parts.push(t('ds_main_data', {
        interval: ds.interval,
        univ: meta.universe_size,
        req: ds.requested_tickers,
        hit: ds.cache_hit_tickers,
        hitRows: ds.cache_hit_rows.toLocaleString(),
        dl: ds.download_tickers,
        dlRows: ds.download_rows.toLocaleString(),
      }));
    }
    if (bs.interval) {
      parts.push(t('ds_bench', {
        interval: bs.interval,
        hit: bs.cache_hit_tickers,
        dl: bs.download_tickers,
      }));
    }
    if (meta.sim_bars) parts.push(t('ds_sim', { bars: meta.sim_bars, interval: meta.interval }));
    dataStatsEl.innerHTML = parts.join('<br>');
  }
  const statsGrid = document.getElementById('bt-stats-grid');
  const metrics = [
    { label: t('m_total_return'), value: formatPercent(s.total_return), cls: s.total_return >= 0 ? 'positive' : 'negative' },
    { label: t('m_max_dd'),       value: formatPercent(s.max_drawdown), cls: 'negative' },
    { label: 'Sharpe',  value: (s.sharpe_ratio || 0).toFixed(2), cls: '' },
    { label: 'Sortino', value: (s.sortino_ratio || 0).toFixed(2), cls: '' },
    { label: t('m_win_rate'),     value: formatPercent(s.win_rate), cls: '' },
    { label: t('m_profit_factor'),value: (s.profit_factor || 0).toFixed(2), cls: '' },
    { label: t('m_total_trades'), value: (s.total_trades || 0).toString(), cls: '' },
  ];
  statsGrid.innerHTML = metrics.map(m => `
    <div class="glass-card bt-stat-box">
      <div class="bt-stat-label">${m.label}</div>
      <div class="bt-stat-value ${m.cls}">${m.value}</div>
    </div>
  `).join('');

  // Equity chart
  renderEquityChart(data);

  // Comparison table
  window.__btResultData = data;  // stash for detail drilldown
  const tbody = document.getElementById('bt-comparison-body');
  tbody.innerHTML = (data.accounts || []).map((a, i) => {
    const st = a.stats || {};
    const retCls = (st.total_return || 0) >= 0 ? 'positive' : 'negative';
    return `<tr class="bt-acct-row" data-idx="${i}" style="cursor:pointer;">
      <td>${a.account_id} <span style="color:var(--text-secondary);font-size:10px;">▸</span></td>
      <td>${tStrategy(a.strategy_name || '', a.account_id)}</td>
      <td class="${retCls}">${formatPercent(st.total_return)}</td>
      <td class="negative">${formatPercent(st.max_drawdown)}</td>
      <td>${(st.sharpe_ratio || 0).toFixed(2)}</td>
      <td>${(st.sortino_ratio || 0).toFixed(2)}</td>
      <td>${formatPercent(st.win_rate)}</td>
      <td>${(st.profit_factor || 0).toFixed(2)}</td>
      <td>${st.total_trades || 0}</td>
    </tr>`;
  }).join('');

  tbody.querySelectorAll('.bt-acct-row').forEach(row => {
    row.addEventListener('click', () => {
      const idx = parseInt(row.dataset.idx, 10);
      openAccountDetail(data.accounts[idx]);
    });
  });
}

let btChart = null;
function renderEquityChart(data) {
  const container = document.getElementById('bt-chart-container');
  container.innerHTML = '';
  if (btChart) { btChart.remove(); btChart = null; }

  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 400,
    layout: { background: { type: 'solid', color: '#ffffff' }, textColor: 'rgba(0,0,0,0.65)', fontSize: 12 },
    grid: { vertLines: { color: 'rgba(0,0,0,0.06)' }, horzLines: { color: 'rgba(0,0,0,0.06)' } },
    crosshair: { mode: 0 },
    timeScale: { borderColor: 'rgba(0,0,0,0.12)', timeVisible: true },
    rightPriceScale: { borderColor: 'rgba(0,0,0,0.12)' },
  });
  btChart = chart;

  const colors = ['#00d4ff', '#7b2ff7', '#00ff88', '#ff4444', '#ffaa00', '#ff66cc', '#66ffcc', '#aaaaff'];

  (data.accounts || []).forEach((a, i) => {
    if (!a.equity_curve || !a.equity_curve.length) return;
    const series = chart.addLineSeries({
      color: colors[i % colors.length],
      lineWidth: 2,
      title: a.account_id,
    });
    const lineData = a.equity_curve.map(p => ({
      time: Math.floor(new Date(p.timestamp).getTime() / 1000),
      value: p.equity,
    }));
    // deduplicate and sort
    const seen = new Set();
    const unique = lineData.filter(d => { if (seen.has(d.time)) return false; seen.add(d.time); return true; });
    unique.sort((a, b) => a.time - b.time);
    series.setData(unique);
  });

  // Benchmark lines (dashed, distinct colors)
  const benchColors = { QQQ: '#f5c518', SPY: '#ffffff' };
  (data.benchmarks || []).forEach(b => {
    if (!b.equity_curve || !b.equity_curve.length) return;
    const series = chart.addLineSeries({
      color: benchColors[b.symbol] || '#888888',
      lineWidth: 2,
      lineStyle: 2,  // dashed
      title: b.symbol + ' ' + t('bench_suffix'),
    });
    const lineData = b.equity_curve.map(p => ({
      time: Math.floor(new Date(p.timestamp).getTime() / 1000),
      value: p.equity,
    }));
    const seen = new Set();
    const unique = lineData.filter(d => { if (seen.has(d.time)) return false; seen.add(d.time); return true; });
    unique.sort((a, b) => a.time - b.time);
    series.setData(unique);
  });

  chart.timeScale().fitContent();

  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);
}


// ===== Account Detail Drilldown =====
let _acctDetailChart = null;
let _acctDetailSeries = null;

function openAccountDetail(acct) {
  // Remove any existing modal
  const existing = document.getElementById('bt-acct-modal');
  if (existing) existing.remove();
  if (_acctDetailChart) { try { _acctDetailChart.remove(); } catch(_){} _acctDetailChart = null; }

  const st = acct.stats || {};
  const initCap = acct.initial_capital || 10000;
  const trades = (acct.trades || []).slice();
  const snapshots = acct.snapshots || [];
  const retCls = (st.total_return || 0) >= 0 ? 'positive' : 'negative';

  // Compute realized PnL for each sell using running avg-cost book.
  (function enrichTrades() {
    const book = {};
    const sorted = trades
      .map((t, i) => ({ t, i, ts: new Date(t.timestamp || 0).getTime() || i }))
      .sort((a, b) => a.ts - b.ts || a.i - b.i);
    for (const { t } of sorted) {
      const tk = t.ticker;
      const side = (t.side || '').toLowerCase();
      const sh = Number(t.shares) || 0;
      const px = Number(t.price) || 0;
      const fees = Number(t.fees) || 0;
      if (!book[tk]) book[tk] = { shares: 0, cost: 0 };
      const b = book[tk];
      if (side === 'buy') {
        b.shares += sh;
        b.cost += sh * px + fees;
      } else if (side === 'sell' && b.shares > 0) {
        const avg = b.cost / b.shares;
        const sold = Math.min(sh, b.shares);
        const pnl = (px - avg) * sold - fees;
        t.realized_pnl = pnl;
        t.realized_pnl_pct = avg > 0 ? (pnl / (avg * sold)) * 100 : 0;
        b.cost -= avg * sold;
        b.shares -= sold;
        if (b.shares < 1e-9) { b.shares = 0; b.cost = 0; }
      }
    }
  })();

  const modal = document.createElement('div');
  modal.id = 'bt-acct-modal';
  modal.innerHTML = `
    <div class="bt-modal-backdrop">
    <div class="bt-modal-panel glass-card">
      <div class="bt-modal-header">
        <div>
          <div style="font-size:16px;font-weight:600;">${acct.account_id}
            <span style="color:var(--text-secondary);font-weight:400;font-size:12px;margin-left:8px;">
              ${tStrategy(acct.strategy_name || '', acct.account_id)}
            </span>
          </div>
          <div style="font-size:11px;color:var(--text-secondary);margin-top:4px;">
            ${t('bt_initial')} ${(typeof currencySymbol === 'function' ? currencySymbol() : '$')}${initCap.toLocaleString()} · ${t('bt_trades_count', {n: trades.length})} ·
            <span class="${retCls}">${t('bt_total_return')} ${formatPercent(st.total_return)}</span> ·
            ${t('bt_max_drawdown')} <span class="negative">${formatPercent(st.max_drawdown)}</span> ·
            Sharpe ${(st.sharpe_ratio||0).toFixed(2)}
          </div>
        </div>
        <button class="bt-modal-close" title="${t('bt_close')}">×</button>
      </div>

      <div class="bt-modal-body">
        <div class="section" style="margin-bottom:12px;">
          <div class="section-title" style="display:flex;justify-content:space-between;align-items:center;">
            <span>${t('detail_equity')}</span>
            <span style="font-size:10px;color:var(--text-tertiary);">${t('bt_hover_hint')}</span>
          </div>
          <div id="bt-acct-chart-wrap" style="position:relative;">
            <div id="bt-acct-chart" style="height:280px;background:#0a0a0f;border-radius:var(--radius-sm);"></div>
            <div id="bt-hover-tip" class="bt-hover-tip" style="display:none;"></div>
          </div>
        </div>

        <div class="section">
          <div class="section-title">
            ${t('bt_trade_details')} (${trades.length})
            <input id="bt-trade-filter" placeholder="${t('bt_filter_ticker')}"
              style="margin-left:12px;padding:3px 8px;background:rgba(0,0,0,0.04);border:1px solid rgba(0,0,0,0.10);border-radius:4px;color:var(--text-primary);font-size:12px;width:140px;">
          </div>
          <div style="max-height:220px;overflow:auto;">
            <table class="data-table" id="bt-trades-table">
              <thead><tr>
                <th>${t('th_time')}</th><th>${t('th_side')}</th><th>${t('th_ticker')}</th>
                <th style="text-align:right;">${t('th_shares')}</th>
                <th style="text-align:right;">${t('th_price')}</th>
                <th style="text-align:right;">${t('th_amount')}</th>
                <th style="text-align:right;">${t('th_fees')}</th>
                <th style="text-align:right;">${t('th_realized_pnl')}</th>
              </tr></thead>
              <tbody id="bt-trades-body"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
    </div>
  `;
  document.body.appendChild(modal);

  const close = () => {
    if (_acctDetailChart) { try { _acctDetailChart.remove(); } catch(_){} _acctDetailChart = null; }
    modal.remove();
  };
  modal.querySelector('.bt-modal-close').addEventListener('click', close);
  modal.querySelector('.bt-modal-backdrop').addEventListener('click', close);
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
  });

  // Equity chart (single account)
  const container = document.getElementById('bt-acct-chart');
  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 280,
    layout: { background: { type: 'solid', color: '#ffffff' }, textColor: 'rgba(0,0,0,0.65)', fontSize: 11 },
    grid: { vertLines: { color: 'rgba(0,0,0,0.06)' }, horzLines: { color: 'rgba(0,0,0,0.06)' } },
    crosshair: { mode: 0 },
    timeScale: { borderColor: 'rgba(0,0,0,0.12)', timeVisible: true },
    rightPriceScale: { borderColor: 'rgba(0,0,0,0.12)' },
  });
  _acctDetailChart = chart;

  const series = chart.addAreaSeries({
    lineColor: '#00d4ff',
    topColor: 'rgba(0,212,255,0.25)',
    bottomColor: 'rgba(0,212,255,0.02)',
    lineWidth: 2,
    priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
  });
  _acctDetailSeries = series;

  const seen = new Set();
  const pts = (acct.equity_curve || []).map(p => ({
    time: Math.floor(new Date(p.timestamp).getTime() / 1000),
    value: p.equity,
  })).filter(p => !Number.isNaN(p.time) && !seen.has(p.time) && seen.add(p.time));
  pts.sort((a, b) => a.time - b.time);
  series.setData(pts);

  // Trade markers on chart
  const tradesByTime = {};
  if (trades.length) {
    const rawMarkers = trades.filter(t => t.timestamp).map(t => {
      const time = Math.floor(new Date(t.timestamp).getTime() / 1000);
      (tradesByTime[time] = tradesByTime[time] || []).push(t);
      return { time, side: (t.side || '').toLowerCase() };
    });
    const byTime = {};
    for (const m of rawMarkers) {
      const k = m.time;
      if (!byTime[k]) byTime[k] = { time: k, buys: 0, sells: 0 };
      if (m.side === 'buy') byTime[k].buys++; else byTime[k].sells++;
    }
    const unique = Object.values(byTime)
      .sort((a, b) => a.time - b.time)
      .map(b => {
        const isBuy = b.buys >= b.sells;
        const n = b.buys + b.sells;
        return {
          time: b.time,
          position: isBuy ? 'belowBar' : 'aboveBar',
          color:    isBuy ? '#00ff88' : '#ff4466',
          shape:    isBuy ? 'arrowUp' : 'arrowDown',
          text: (isBuy ? 'B' : 'S') + (n > 1 ? '·' + n : ''),
        };
      });
    if (unique.length && unique.length < 2000) series.setMarkers(unique);
  }

  chart.timeScale().fitContent();

  const tipEl = document.getElementById('bt-hover-tip');
  const wrapEl = document.getElementById('bt-acct-chart-wrap');

  function fmtMoney(v) {
    return ((typeof currencySymbol === 'function') ? currencySymbol() : '$') + Number(v || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  function renderTradeTip(ts, list) {
    const rows = list.map(tr => {
      const side = (tr.side || '').toUpperCase();
      const sideC = side === 'BUY' ? 'positive' : 'negative';
      let pnlHtml = '';
      if (side === 'SELL' && tr.realized_pnl != null) {
        const c = tr.realized_pnl >= 0 ? 'positive' : 'negative';
        const sign = tr.realized_pnl >= 0 ? '+' : '';
        pnlHtml = `<div class="tip-pnl ${c}">${t('bt_pnl_label')} ${sign}${fmtMoney(tr.realized_pnl)} (${sign}${(tr.realized_pnl_pct||0).toFixed(2)}%)</div>`;
      }
      return `
        <div class="tip-trade">
          <div class="tip-trade-head">
            <span class="${sideC}" style="font-weight:600;">${side}</span>
            <span class="tip-ticker">${tr.ticker || ''}</span>
          </div>
          <div class="tip-trade-meta">${tr.shares} × ${fmtMoney(tr.price)} = ${fmtMoney(tr.amount)}</div>
          ${pnlHtml}
        </div>`;
    }).join('');
    tipEl.innerHTML = `<div class="tip-ts">${ts}</div>${rows}`;
  }

  function renderSnapshotTip(snap) {
    if (!snap) { tipEl.style.display = 'none'; return; }
    const eq = snap.equity || 0;
    const ret = initCap ? ((eq / initCap - 1) * 100) : 0;
    const retC = ret >= 0 ? 'positive' : 'negative';
    const head = `
      <div class="tip-ts">${snap.timestamp}</div>
      <div class="tip-summary">
        ${t('bt_equity_label')} ${fmtMoney(eq)} · ${t('bt_cash')} ${fmtMoney(snap.cash)}
        · <span class="${retC}">${t('bt_cumulative')} ${ret.toFixed(2)}%</span>
      </div>`;
    if (!snap.holdings || !snap.holdings.length) {
      tipEl.innerHTML = head + `<div class="tip-empty">${t('bt_no_positions')}</div>`;
      return;
    }
    const sorted = snap.holdings.slice().sort((a,b) => (b.value||0) - (a.value||0));
    const shown = sorted.slice(0, 10);
    const hidden = sorted.length - shown.length;
    const body = shown.map(h => {
      const pnlC = (h.pnl_pct || 0) >= 0 ? 'positive' : 'negative';
      const sign = (h.pnl_pct || 0) >= 0 ? '+' : '';
      return `
        <div class="tip-hold">
          <span class="tip-ticker">${h.ticker}</span>
          <span class="tip-hold-meta">${h.shares}×${fmtMoney(h.price)} / ${t('bt_cost_label')} ${fmtMoney(h.avg_cost)}</span>
          <span class="${pnlC} tip-hold-pnl">${sign}${(h.pnl_pct||0).toFixed(2)}%</span>
        </div>`;
    }).join('');
    const more = hidden > 0 ? `<div class="tip-empty">${t('bt_more_items', {n: hidden})}</div>` : '';
    tipEl.innerHTML = head + body + more;
  }

  const snapByTs = {};
  const snapKeys = [];
  snapshots.forEach(s => {
    const tt = Math.floor(new Date(s.timestamp).getTime() / 1000);
    snapByTs[tt] = s;
    snapKeys.push(tt);
  });
  snapKeys.sort((a,b) => a - b);

  chart.subscribeCrosshairMove(param => {
    if (!param || !param.time || !param.point) {
      tipEl.style.display = 'none';
      return;
    }
    const tt = typeof param.time === 'number' ? param.time : null;
    if (tt === null) { tipEl.style.display = 'none'; return; }

    const tradeList = tradesByTime[tt];
    if (tradeList && tradeList.length) {
      const ts = tradeList[0].timestamp || '';
      renderTradeTip(ts, tradeList);
    } else {
      let snap = snapByTs[tt];
      if (!snap) {
        let lo = 0, hi = snapKeys.length - 1, best = -1;
        while (lo <= hi) {
          const mid = (lo + hi) >> 1;
          if (snapKeys[mid] <= tt) { best = snapKeys[mid]; lo = mid + 1; } else hi = mid - 1;
        }
        if (best >= 0) snap = snapByTs[best];
      }
      if (!snap) { tipEl.style.display = 'none'; return; }
      renderSnapshotTip(snap);
    }

    tipEl.style.display = 'block';
    const wrapW = wrapEl.clientWidth;
    const wrapH = wrapEl.clientHeight;
    const tipW = tipEl.offsetWidth || 260;
    const tipH = tipEl.offsetHeight || 120;
    let left = param.point.x + 14;
    let top = param.point.y + 14;
    if (left + tipW > wrapW - 6) left = param.point.x - tipW - 14;
    if (top + tipH > wrapH - 6)  top = Math.max(6, wrapH - tipH - 6);
    if (left < 6) left = 6;
    if (top < 6) top = 6;
    tipEl.style.left = left + 'px';
    tipEl.style.top  = top + 'px';
  });

  container.addEventListener('mouseleave', () => { tipEl.style.display = 'none'; });

  // Trades table
  const body = document.getElementById('bt-trades-body');
  function renderTrades(filter) {
    const f = (filter || '').trim().toUpperCase();
    const rows = trades
      .filter(tr => !f || (tr.ticker || '').includes(f))
      .map(tr => {
        const side = (tr.side || '').toUpperCase();
        const sideCls = side === 'BUY' ? 'positive' : 'negative';
        let pnlCell = '<td style="text-align:right;color:var(--text-tertiary);">—</td>';
        if (side === 'SELL' && tr.realized_pnl != null) {
          const c = tr.realized_pnl >= 0 ? 'positive' : 'negative';
          const sign = tr.realized_pnl >= 0 ? '+' : '';
          pnlCell = `<td style="text-align:right;font-family:monospace;" class="${c}">${sign}${fmtMoney(tr.realized_pnl)} (${sign}${(tr.realized_pnl_pct||0).toFixed(2)}%)</td>`;
        }
        return `<tr>
          <td style="font-family:monospace;font-size:11px;">${tr.timestamp || ''}</td>
          <td class="${sideCls}">${side}</td>
          <td>${tr.ticker || ''}</td>
          <td style="text-align:right;font-family:monospace;">${tr.shares}</td>
          <td style="text-align:right;font-family:monospace;">${fmtMoney(tr.price)}</td>
          <td style="text-align:right;font-family:monospace;">${fmtMoney(tr.amount||0)}</td>
          <td style="text-align:right;font-family:monospace;color:var(--text-secondary);">${fmtMoney(tr.fees)}</td>
          ${pnlCell}
        </tr>`;
      }).join('');
    body.innerHTML = rows || `<tr><td colspan="8" style="text-align:center;color:var(--text-secondary);padding:20px;">${t('bt_no_trades')}</td></tr>`;
  }
  renderTrades('');
  document.getElementById('bt-trade-filter').addEventListener('input', e => renderTrades(e.target.value));

  const ro = new ResizeObserver(() => chart.applyOptions({ width: container.clientWidth }));
  ro.observe(container);
}
