// app.js — SPA router and utilities

// --- Global app state ---
const state = {
  market: 'US',  // 'US' | 'CN' — drives currency formatting and ?market= on every API call
};

// Initialize market from URL ?market= (US default).
(function initMarketFromUrl() {
  try {
    const params = new URLSearchParams(location.search);
    const m = (params.get('market') || 'US').toUpperCase();
    if (m === 'US' || m === 'CN') state.market = m;
  } catch (e) { /* noop */ }
})();

function currencySymbol(market) {
  return (market || state.market) === 'CN' ? '¥' : '$';
}

// --- Utilities ---
function formatMoney(n, market) {
  if (n == null || isNaN(n)) return currencySymbol(market) + '0.00';
  const sym = currencySymbol(market);
  const abs = Math.abs(n);
  const formatted = abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (n < 0 ? '-' + sym : sym) + formatted;
}

// Back-compat alias — every call site that previously assumed USD now respects state.market.
function formatCurrency(n) { return formatMoney(n, state.market); }

// --- Ticker → human name (CN only for now) ---
// Cache of {ticker: {cn, en}} loaded once per market switch from /api/trade/ticker-names.
const _tickerNameCache = { US: {}, CN: null };  // null = not yet loaded

async function loadTickerNames(market) {
  market = (market || state.market).toUpperCase();
  if (_tickerNameCache[market]) return _tickerNameCache[market];
  try {
    const lang = (typeof getLang === 'function') ? getLang() : 'en';
    // bypass api() helper to avoid double market param
    const res = await fetch(`/api/trade/ticker-names?market=${market}&lang=${lang}`);
    _tickerNameCache[market] = res.ok ? await res.json() : {};
  } catch (e) {
    _tickerNameCache[market] = {};
  }
  return _tickerNameCache[market];
}

// Synchronous lookup. Returns 'TICKER name' if cached, else 'TICKER'.
// Components should pre-load via loadTickerNames() before first paint.
function formatTicker(ticker, opts) {
  if (!ticker) return '';
  const market = (opts && opts.market) || state.market;
  const cache = _tickerNameCache[market];
  if (!cache) return ticker;
  const meta = cache[ticker];
  if (!meta) return ticker;
  const lang = (typeof getLang === 'function') ? getLang() : 'en';
  const name = (lang === 'zh' || lang === 'cn') ? (meta.cn || meta.en) : (meta.en || meta.cn);
  return name ? `${ticker} ${name}` : ticker;
}
window.formatTicker = formatTicker;
window.loadTickerNames = loadTickerNames;

function formatPercent(n) {
  if (n == null || isNaN(n)) return '0.00%';
  return n.toFixed(2) + '%';
}

function formatDate(s) {
  if (!s) return '—';
  try {
    const d = new Date(s);
    return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }) + ' ' +
           d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  } catch { return s; }
}

function animateNumber(el, target, duration, prefix) {
  if (!el) return;
  prefix = prefix || '';
  const start = 0;
  const startTime = performance.now();
  function tick(now) {
    const elapsed = now - startTime;
    const progress = Math.min(elapsed / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = start + (target - start) * eased;
    el.textContent = prefix + Math.abs(current).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

async function api(path) {
  const lang = (typeof getLang === 'function') ? getLang() : 'en';
  const sep = path.includes('?') ? '&' : '?';
  const url = '/api' + path + sep + 'lang=' + lang + '&market=' + encodeURIComponent(state.market);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}

// --- Router ---
const routes = {
  '/trade': renderTradePage,
  '/backtest': renderBacktestPage,
  '/explore': renderExplorePage,
  '/frontier': renderFrontierPage,
  '/symbols': renderSymbolsPage,
  '/intro': renderIntroPage,
};

function navigate() {
  const hash = location.hash.replace('#', '') || '/trade';
  const app = document.getElementById('app');

  // Detect explore post: #/explore/<slug>
  const exploreMatch = hash.match(/^\/explore\/(.+)$/);
  // Detect frontier paper: #/frontier/<arxiv_id>
  const frontierMatch = hash.match(/^\/frontier\/(.+)$/);
  // Detect symbol detail: #/symbols/<ticker>
  const symbolMatch = hash.match(/^\/symbols\/(.+)$/);
  const navKey = exploreMatch ? '/explore'
    : (frontierMatch ? '/frontier'
    : (symbolMatch ? '/symbols' : hash));

  document.querySelectorAll('.nav-link').forEach(link => {
    link.classList.toggle('active', link.getAttribute('href') === '#' + navKey);
  });

  app.classList.add('fade-out');
  setTimeout(() => {
    app.classList.remove('fade-out');
    app.classList.add('fade-in');
    if (exploreMatch) {
      renderExplorePost(decodeURIComponent(exploreMatch[1]));
      return;
    }
    if (frontierMatch) {
      renderFrontierPost(decodeURIComponent(frontierMatch[1]));
      return;
    }
    if (symbolMatch) {
      renderSymbolDetail(decodeURIComponent(symbolMatch[1]));
      return;
    }
    const handler = routes[hash];
    if (handler) handler();
    else renderTradePage();
  }, 200);
}

// --- Market tabs ---
function setMarket(m) {
  m = (m || 'US').toUpperCase();
  if (m !== 'US' && m !== 'CN') return;
  if (m === state.market) return;
  state.market = m;
  // Update URL ?market= without reloading
  try {
    const u = new URL(location.href);
    u.searchParams.set('market', m);
    history.replaceState(null, '', u.toString());
  } catch (e) { /* noop */ }
  paintMarketUI();
  // Re-load ticker names for new market (lazy in loadTickerNames; invalidate stale cache)
  loadTickerNames(state.market).catch(() => {});
  // Re-render the active route — every panel re-fetches via api() which now appends &market=
  navigate();
}

function paintMarketUI() {
  document.querySelectorAll('.market-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.market === state.market);
  });
  const badge = document.getElementById('market-badge');
  if (badge) badge.textContent = state.market + ' · ' + currencySymbol();
}

function bindMarketTabs() {
  document.querySelectorAll('.market-tab').forEach(btn => {
    if (btn._bound) return;
    btn._bound = true;
    btn.addEventListener('click', () => setMarket(btn.dataset.market));
  });
}

window.addEventListener('hashchange', navigate);
window.addEventListener('DOMContentLoaded', () => {
  bindMarketTabs();
  paintMarketUI();
  if (!location.hash) location.hash = '#/trade';
  else navigate();
});
