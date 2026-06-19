// factor_lab.js — ad-hoc Alpha158 factor research workbench
//
// Account-free by design: build a temporary factor composite from Alpha158
// building blocks, run look-ahead-safe IC + simple top-N portfolio diagnostics.

(function () {
  const STORE_KEY = 'cqa_factor_lab_terms_v1';
  const _state = {
    catalog: [],
    terms: [],
    lastResult: null,
  };
  let equityChart = null;
  let icChart = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
  }

  function pct(v, digits) {
    if (v == null || Number.isNaN(Number(v))) return '—';
    return Number(v).toFixed(digits == null ? 2 : digits) + '%';
  }

  function num(v, digits) {
    if (v == null || Number.isNaN(Number(v))) return '—';
    return Number(v).toFixed(digits == null ? 2 : digits);
  }

  function pickFactorLabel(f) {
    const lang = (typeof getLang === 'function') ? getLang() : 'en';
    return lang === 'zh' ? (f.label_zh || f.label_en || f.name) : (f.label_en || f.label_zh || f.name);
  }

  function pickFactorDesc(f) {
    const lang = (typeof getLang === 'function') ? getLang() : 'en';
    return lang === 'zh' ? (f.description_zh || f.description_en || '') : (f.description_en || f.description_zh || '');
  }

  function defaultTerms() {
    return [
      { factor: 'ROC_20', weight: 0.4, transform: 'rank' },
      { factor: 'MA_RATIO_20', weight: 0.3, transform: 'rank' },
      { factor: 'RSI_14', weight: -0.2, transform: 'rank' },
      { factor: 'VSTD_20', weight: 0.1, transform: 'rank' },
    ];
  }

  function loadSavedTerms() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
      if (Array.isArray(raw) && raw.length) return raw;
    } catch (e) { /* noop */ }
    return defaultTerms();
  }

  function saveTerms() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(_state.terms)); } catch (e) { /* noop */ }
  }

  function catalogByName() {
    const m = {};
    (_state.catalog || []).forEach(f => { m[f.name] = f; });
    return m;
  }

  async function renderFactorLabPage() {
    const app = document.getElementById('app');
    app.innerHTML = `
      <div class="factor-lab-page fade-in">
        <div class="page-hero factor-lab-hero">
          <h1 class="page-title">${t('fl_title')}</h1>
          <p class="page-subtitle">${t('fl_subtitle')}</p>
        </div>
        <div class="factor-lab-layout">
          <div class="glass-card section factor-lab-config">
            <div class="section-title">${t('fl_builder')}</div>
            <div class="fl-method-note">${t('fl_method_note')}</div>
            <div class="fl-field-row">
              <div class="bt-field">
                <label>${t('bt_start_date')}</label>
                <input type="date" id="fl-start" class="bt-input">
              </div>
              <div class="bt-field">
                <label>${t('bt_end_date')}</label>
                <input type="date" id="fl-end" class="bt-input">
              </div>
            </div>
            <div class="fl-field-row">
              <div class="bt-field">
                <label>${t('fl_universe_size')}</label>
                <input type="number" id="fl-universe" class="bt-input" min="10" max="1200" value="300">
              </div>
              <div class="bt-field">
                <label>${t('fl_top_n')}</label>
                <input type="number" id="fl-topn" class="bt-input" min="1" max="500" value="20">
              </div>
            </div>
            <div class="fl-field-row">
              <div class="bt-field">
                <label>${t('bt_initial_capital')}</label>
                <input type="number" id="fl-capital" class="bt-input" value="${state.market === 'CN' ? 100000 : 10000}">
              </div>
              <div class="bt-field">
                <label>${t('fl_cost_bps')}</label>
                <input type="number" id="fl-cost" class="bt-input" min="0" max="100" step="0.5" value="5">
              </div>
            </div>
            <div class="fl-field-row">
              <div class="bt-field">
                <label>${t('fl_rebalance')}</label>
                <select id="fl-rebalance" class="bt-input">
                  <option value="daily">${t('fl_daily')}</option>
                  <option value="weekly" selected>${t('fl_weekly')}</option>
                  <option value="monthly">${t('fl_monthly')}</option>
                </select>
              </div>
              <div class="bt-field">
                <label>${t('fl_horizon')}</label>
                <select id="fl-horizon" class="bt-input">
                  <option value="1">1D</option>
                  <option value="5" selected>5D</option>
                  <option value="10">10D</option>
                  <option value="20">20D</option>
                </select>
              </div>
            </div>

            <div class="fl-term-header">
              <div class="section-title fl-mini-title">${t('fl_expression')}</div>
              <button class="btn btn-secondary fl-small-btn" id="fl-reset">${t('fl_reset_sample')}</button>
            </div>
            <div id="fl-terms" class="fl-terms"></div>
            <button class="btn btn-secondary fl-add-term" id="fl-add-term">＋ ${t('fl_add_factor')}</button>
            <button class="btn btn-accent bt-run-btn fl-run" id="fl-run">${t('fl_run')}</button>
          </div>

          <div class="factor-lab-main">
            <div class="glass-card section factor-catalog-card">
              <div class="section-title-row">
                <div class="section-title">${t('fl_catalog')}</div>
                <input id="fl-factor-search" class="sym-search-input fl-search" placeholder="${t('fl_search_factor')}" autocomplete="off">
              </div>
              <div id="fl-catalog-host" class="fl-catalog-host"><p class="fl-muted">${t('bt_loading')}</p></div>
            </div>
            <div id="fl-result-host" class="factor-lab-results">
              <div class="glass-card section fl-empty">
                <div class="fl-empty-icon">🧪</div>
                <div class="fl-empty-title">${t('fl_empty_title')}</div>
                <p>${t('fl_empty_desc')}</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;

    _state.terms = loadSavedTerms();
    setDefaultDates();
    bindStaticControls();
    paintTerms();

    try {
      const res = await fetch(`/api/factor-lab/catalog?market=${encodeURIComponent(state.market)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      _state.catalog = data.factors || [];
      const defaults = data.defaults || {};
      document.getElementById('fl-capital').value = defaults.initial_capital || (state.market === 'CN' ? 100000 : 10000);
      document.getElementById('fl-universe').value = defaults.universe_size || 300;
      paintCatalog();
      paintTerms();
    } catch (e) {
      document.getElementById('fl-catalog-host').innerHTML = `<p style="color:var(--negative);padding:12px;">${t('bt_load_failed')} ${esc(e.message)}</p>`;
    }
  }

  function setDefaultDates() {
    const end = new Date();
    const start = new Date(Date.now() - 90 * 24 * 3600 * 1000);
    const fmt = d => d.toISOString().slice(0, 10);
    document.getElementById('fl-start').value = fmt(start);
    document.getElementById('fl-end').value = fmt(end);
  }

  function bindStaticControls() {
    document.getElementById('fl-add-term').addEventListener('click', () => {
      _state.terms.push({ factor: (_state.catalog[0] && _state.catalog[0].name) || 'ROC_20', weight: 1, transform: 'rank' });
      saveTerms();
      paintTerms();
    });
    document.getElementById('fl-reset').addEventListener('click', () => {
      _state.terms = defaultTerms();
      saveTerms();
      paintTerms();
    });
    document.getElementById('fl-run').addEventListener('click', runFactorLab);
    const search = document.getElementById('fl-factor-search');
    search.addEventListener('input', paintCatalog);
  }

  function paintTerms() {
    const host = document.getElementById('fl-terms');
    const opts = (_state.catalog.length ? _state.catalog : defaultTerms().map(t => ({ name: t.factor })))
      .map(f => `<option value="${esc(f.name)}">${esc(f.name)}${f.label_zh ? ' · ' + esc(pickFactorLabel(f)) : ''}</option>`).join('');
    host.innerHTML = _state.terms.map((term, i) => `
      <div class="fl-term-row" data-idx="${i}">
        <select class="bt-input fl-term-factor" data-field="factor">${opts}</select>
        <input type="number" class="bt-input fl-term-weight" data-field="weight" step="0.1" value="${Number(term.weight || 0)}">
        <select class="bt-input fl-term-transform" data-field="transform">
          <option value="rank">rank</option>
          <option value="zscore">zscore</option>
        </select>
        <button class="fl-term-remove" title="${t('fl_remove')}">×</button>
      </div>
    `).join('');
    host.querySelectorAll('.fl-term-row').forEach(row => {
      const idx = parseInt(row.dataset.idx, 10);
      const f = row.querySelector('[data-field="factor"]');
      const w = row.querySelector('[data-field="weight"]');
      const tr = row.querySelector('[data-field="transform"]');
      f.value = _state.terms[idx].factor;
      tr.value = _state.terms[idx].transform || 'rank';
      [f, w, tr].forEach(el => el.addEventListener('change', () => {
        _state.terms[idx] = { factor: f.value, weight: parseFloat(w.value) || 0, transform: tr.value };
        saveTerms();
      }));
      row.querySelector('.fl-term-remove').addEventListener('click', () => {
        _state.terms.splice(idx, 1);
        saveTerms();
        paintTerms();
      });
    });
  }

  function paintCatalog() {
    const host = document.getElementById('fl-catalog-host');
    const f = (document.getElementById('fl-factor-search')?.value || '').trim().toLowerCase();
    let rows = (_state.catalog || []).filter(x => {
      if (!f) return true;
      return (x.name || '').toLowerCase().includes(f)
        || (x.family || '').toLowerCase().includes(f)
        || (x.label_zh || '').toLowerCase().includes(f)
        || (x.label_en || '').toLowerCase().includes(f);
    });
    if (!rows.length) {
      host.innerHTML = `<p class="fl-muted">${t('fl_no_factor')}</p>`;
      return;
    }
    const fams = {};
    rows.forEach(x => { if (!fams[x.family]) fams[x.family] = []; fams[x.family].push(x); });
    host.innerHTML = Object.entries(fams).map(([family, xs]) => `
      <div class="fl-family">
        <div class="fl-family-title">${esc(family)}</div>
        <div class="fl-factor-grid">
          ${xs.map(x => {
            const cov = x.coverage || {};
            return `<button class="fl-factor-chip" data-factor="${esc(x.name)}">
              <span class="fl-factor-name">${esc(x.name)}</span>
              <span class="fl-factor-label">${esc(pickFactorLabel(x))}</span>
              <span class="fl-factor-desc">${esc(pickFactorDesc(x))}</span>
              <span class="fl-factor-cov">${esc(cov.min_date || '—')} → ${esc(cov.max_date || '—')}</span>
            </button>`;
          }).join('')}
        </div>
      </div>
    `).join('');
    host.querySelectorAll('.fl-factor-chip').forEach(btn => {
      btn.addEventListener('click', () => {
        _state.terms.push({ factor: btn.dataset.factor, weight: 1, transform: 'rank' });
        saveTerms();
        paintTerms();
      });
    });
  }

  async function runFactorLab() {
    const btn = document.getElementById('fl-run');
    const host = document.getElementById('fl-result-host');
    const body = {
      market: state.market,
      start_date: document.getElementById('fl-start').value,
      end_date: document.getElementById('fl-end').value,
      initial_capital: parseFloat(document.getElementById('fl-capital').value) || (state.market === 'CN' ? 100000 : 10000),
      universe_size: parseInt(document.getElementById('fl-universe').value, 10) || 300,
      top_n: parseInt(document.getElementById('fl-topn').value, 10) || 20,
      cost_bps: parseFloat(document.getElementById('fl-cost').value) || 0,
      rebalance: document.getElementById('fl-rebalance').value,
      horizon: parseInt(document.getElementById('fl-horizon').value, 10) || 5,
      window: 20,
      expression: { terms: _state.terms.filter(t => t.factor && Number(t.weight) !== 0), final_transform: 'rank' },
    };
    if (!body.start_date || !body.end_date) { alert(t('bt_pick_dates')); return; }
    if (!body.expression.terms.length) { alert(t('fl_need_factor')); return; }
    btn.disabled = true;
    btn.textContent = t('fl_running');
    host.innerHTML = `
      <div class="glass-card section fl-running-card">
        <div class="fl-spinner"></div>
        <div>
          <div class="fl-empty-title">${t('fl_running')}</div>
          <p>${t('fl_running_desc')}</p>
        </div>
      </div>`;
    try {
      const res = await fetch('/api/factor-lab/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try { const j = await res.json(); detail = j.detail || detail; } catch (e) {}
        throw new Error(detail);
      }
      const data = await res.json();
      _state.lastResult = data;
      paintResults(data);
    } catch (e) {
      host.innerHTML = `<div class="glass-card section"><p style="color:var(--negative);">${t('bt_error_prefix')} ${esc(e.message)}</p></div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = t('fl_run');
    }
  }

  function paintResults(data) {
    const host = document.getElementById('fl-result-host');
    const s = data.summary || {};
    const meta = data.meta || {};
    const cov = data.coverage || {};
    const retCls = (Number(s.total_return_pct) || 0) >= 0 ? 'positive' : 'negative';
    host.innerHTML = `
      <div class="glass-card section fl-result-card">
        <div class="section-title-row">
          <div>
            <div class="section-title">${t('fl_result_title')}</div>
            <div class="fl-result-meta">${esc(meta.start_date)} → ${esc(meta.end_date)} · ${esc(meta.rebalance)} · universe ${cov.priced_universe || cov.selected_universe || '—'} · top ${meta.top_n}</div>
          </div>
        </div>
        <div class="bt-stats-grid fl-stats-grid">
          ${statBox(t('m_total_return'), pct(s.total_return_pct), retCls)}
          ${statBox(t('m_max_dd'), pct(s.max_drawdown_pct), 'negative')}
          ${statBox('Sharpe', num(s.sharpe, 2), '')}
          ${statBox(t('sq_mean_ic'), num(s.mean_ic, 4), (s.mean_ic || 0) >= 0 ? 'positive' : 'negative')}
          ${statBox(t('sq_icir'), num(s.icir, 2), '')}
          ${statBox(t('sq_win_rate'), pct((s.ic_win_rate || 0) * 100), '')}
          ${statBox(t('fl_turnover'), pct(s.avg_turnover_pct), '')}
          ${statBox(t('sq_n_days'), String(s.n_ic_days || 0), '')}
        </div>
        <div class="fl-chart-grid">
          <div>
            <div class="fl-chart-title">${t('fl_equity_chart')}</div>
            <div id="fl-equity-chart" class="glass-card fl-chart-box"></div>
          </div>
          <div>
            <div class="fl-chart-title">${t('fl_ic_chart')}</div>
            <div id="fl-ic-chart" class="glass-card fl-chart-box"></div>
          </div>
        </div>
        <div class="fl-bottom-grid">
          <div>
            <div class="fl-chart-title">${t('fl_quantiles')}</div>
            ${renderQuantiles(data.quantile_returns || [])}
          </div>
          <div>
            <div class="fl-chart-title">${t('fl_latest_holdings')}</div>
            ${renderHoldings(data.latest_holdings || [])}
          </div>
        </div>
        <div class="fl-warnings">
          ${(((typeof getLang === 'function' && getLang() === 'en') ? (data.warnings_en || data.warnings) : data.warnings) || []).map(w => `<div class="fl-warning">⚠️ ${esc(w)}</div>`).join('')}
        </div>
      </div>
    `;
    requestAnimationFrame(() => {
      renderEquity(data.equity_curve || []);
      renderIC(data.ic_series || []);
    });
  }

  function statBox(label, value, cls) {
    return `<div class="glass-card bt-stat-box"><div class="bt-stat-label">${esc(label)}</div><div class="bt-stat-value ${cls || ''}">${esc(value)}</div></div>`;
  }

  function renderQuantiles(rows) {
    if (!rows.length) return `<p class="fl-muted">${t('sq_no_data')}</p>`;
    const vals = rows.map(r => Number(r.mean_forward_return_pct) || 0);
    const maxAbs = Math.max(0.01, ...vals.map(v => Math.abs(v)));
    return `<div class="fl-qbars">${rows.map(r => {
      const v = Number(r.mean_forward_return_pct) || 0;
      const w = Math.min(100, Math.abs(v) / maxAbs * 100);
      const cls = v >= 0 ? 'positive' : 'negative';
      return `<div class="fl-qbar-row"><span>Q${r.quantile}</span><div class="fl-qbar-track"><div class="fl-qbar ${cls}" style="width:${w}%"></div></div><b class="${cls}">${pct(v, 3)}</b></div>`;
    }).join('')}</div>`;
  }

  function renderHoldings(rows) {
    if (!rows.length) return `<p class="fl-muted">${t('no_positions')}</p>`;
    return `<div class="fl-holdings-table">
      ${rows.slice(0, 20).map(r => `<div class="fl-holding-row"><span>${esc(formatTicker ? formatTicker(r.ticker) : r.ticker)}</span><b>${num(r.score, 4)}</b><em>${pct(r.weight_pct)}</em></div>`).join('')}
    </div>`;
  }

  function renderEquity(curve) {
    const container = document.getElementById('fl-equity-chart');
    if (!container) return;
    container.innerHTML = '';
    if (equityChart) { try { equityChart.remove(); } catch (e) {} equityChart = null; }
    if (!curve.length || typeof LightweightCharts === 'undefined') return;
    const chart = LightweightCharts.createChart(container, {
      width: container.clientWidth,
      height: 300,
      layout: { background: { type: 'solid', color: '#ffffff' }, textColor: 'rgba(0,0,0,0.65)', fontSize: 12 },
      grid: { vertLines: { color: 'rgba(0,0,0,0.06)' }, horzLines: { color: 'rgba(0,0,0,0.06)' } },
      timeScale: { borderColor: 'rgba(0,0,0,0.12)' },
      rightPriceScale: { borderColor: 'rgba(0,0,0,0.12)' },
    });
    equityChart = chart;
    const series = chart.addLineSeries({ color: '#0071e3', lineWidth: 3, title: t('bt_equity_label') });
    series.setData(curve.map(p => ({ time: p.date, value: Number(p.equity) })).filter(p => p.time && Number.isFinite(p.value)));
    chart.timeScale().fitContent();
    requestAnimationFrame(() => chart.timeScale().fitContent());
    new ResizeObserver(() => { chart.applyOptions({ width: container.clientWidth }); chart.timeScale().fitContent(); }).observe(container);
  }

  function renderIC(rows) {
    const container = document.getElementById('fl-ic-chart');
    if (!container) return;
    container.innerHTML = '';
    if (icChart) { try { icChart.remove(); } catch (e) {} icChart = null; }
    if (!rows.length || typeof LightweightCharts === 'undefined') return;
    const chart = LightweightCharts.createChart(container, {
      width: container.clientWidth,
      height: 300,
      layout: { background: { type: 'solid', color: '#ffffff' }, textColor: 'rgba(0,0,0,0.65)', fontSize: 12 },
      grid: { vertLines: { color: 'rgba(0,0,0,0.06)' }, horzLines: { color: 'rgba(0,0,0,0.06)' } },
      timeScale: { borderColor: 'rgba(0,0,0,0.12)' },
      rightPriceScale: { borderColor: 'rgba(0,0,0,0.12)' },
    });
    icChart = chart;
    const icSeries = chart.addHistogramSeries({ color: '#0071e3', priceFormat: { type: 'price', precision: 3, minMove: 0.001 } });
    icSeries.setData(rows.map(r => ({ time: r.date, value: Number(r.ic), color: Number(r.ic) >= 0 ? 'rgba(31,157,85,0.85)' : 'rgba(224,45,45,0.85)' })).filter(p => p.time && Number.isFinite(p.value)));
    const roll = chart.addLineSeries({ color: '#ac39ff', lineWidth: 2, title: t('sq_rolling_icir') });
    roll.setData(rows.map(r => ({ time: r.date, value: Number(r.rolling_icir) })).filter(p => p.time && Number.isFinite(p.value)));
    chart.timeScale().fitContent();
    requestAnimationFrame(() => chart.timeScale().fitContent());
    new ResizeObserver(() => { chart.applyOptions({ width: container.clientWidth }); chart.timeScale().fitContent(); }).observe(container);
  }

  window.renderFactorLabPage = renderFactorLabPage;
})();
