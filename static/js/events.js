// events.js — Live system event stream
// • Initial 100 newest, no hard cap; scroll-to-bottom loads 50 older.
// • Polling re-fetches the top page only — newer events naturally rise to top.
// • [系统] events come from git commits on this dashboard repo (server-injected).

const EVENT_INITIAL = 100;
const EVENT_PAGE = 50;
const EVENT_POLL_MS = 4000;

const EVENT_STYLE = {
  system:    { dot: 'var(--accent-purple)', label: 'events_cat_system' },
  data:      { dot: 'var(--accent-blue)',   label: 'events_cat_data' },
  factor:    { dot: 'var(--accent-blue)',   label: 'events_cat_factor' },
  rebalance: { dot: 'var(--accent-blue)',   label: 'events_cat_rebalance' },
  trade:     { dot: 'var(--accent-blue)',   label: 'events_cat_trade' },
  risk:      { dot: '#ff7043',              label: 'events_cat_risk' },
  guard:     { dot: '#ff7043',              label: 'events_cat_guard' },
  lifecycle: { dot: '#ffb74d',              label: 'events_cat_lifecycle' },
  inception: { dot: '#4caf50',              label: 'events_cat_inception' },
};

let _eventsState = {
  items: [],          // newest first
  knownIds: new Set(),
  timer: null,
  loadingMore: false,
  exhausted: false,   // server returned 0 older items
  scrollBound: false,
};

function eventsSectionHtml() {
  return `
    <div class="section" id="events-section">
      <div class="section-title-row">
        <div class="section-title">${t('events_title')}</div>
        <div class="events-status">
          <span class="live-dot"></span>
          <span class="live-text" data-i18n="nav_live">${t('nav_live')}</span>
        </div>
      </div>
      <div class="glass-card events-card" id="events-card">
        <div class="events-list" id="events-list">
          <div class="events-empty">${t('events_loading')}</div>
        </div>
        <div class="events-footer" id="events-footer" style="display:none;"></div>
      </div>
    </div>
  `;
}

function fmtEventTime(iso) {
  try {
    const d = new Date(iso);
    const now = new Date();
    const sameDay = d.getFullYear() === now.getFullYear()
                 && d.getMonth() === now.getMonth()
                 && d.getDate() === now.getDate();
    const pad = n => String(n).padStart(2, '0');
    const hh = pad(d.getHours()), mm = pad(d.getMinutes()), ss = pad(d.getSeconds());
    if (sameDay) return `${hh}:${mm}:${ss}`;
    return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${hh}:${mm}`;
  } catch { return iso || ''; }
}

function eventRowHtml(ev, isNew) {
  const style = EVENT_STYLE[ev.category] || EVENT_STYLE.system;
  const time = fmtEventTime(ev.ts);
  const tickerLabel = ev.ticker
    ? (typeof formatTicker === 'function' ? formatTicker(ev.ticker) : ev.ticker)
    : '';
  const tag = (ev.account || ev.ticker)
    ? `<span class="ev-tag">${[ev.account, tickerLabel].filter(Boolean).join(' · ')}</span>`
    : '';
  const catLabel = t(style.label) || ev.category;
  const sevClass = ev.severity && ev.severity !== 'info' ? ` ev-sev-${ev.severity}` : '';
  const escape = s => String(s || '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
  const safeTitle = escape(ev.title);
  // Detail line: parse as JSON if possible, surface .reason; plain text → 2nd line.
  let detailLine = '';
  if (ev.detail) {
    try {
      const d = JSON.parse(ev.detail);
      if (d && d.reason) {
        detailLine = `<span class="ev-detail">↳ ${escape(d.reason)}</span>`;
      }
    } catch {
      const first = String(ev.detail).split('\n').slice(1, 2)[0] || '';
      if (first) detailLine = `<span class="ev-detail">↳ ${escape(first.slice(0,200))}</span>`;
    }
  }
  return `
    <div class="ev-row${isNew ? ' ev-new' : ''}${sevClass}" data-id="${ev.id}">
      <span class="ev-dot" style="background:${style.dot};box-shadow:0 0 8px ${style.dot};"></span>
      <span class="ev-time">[${time}]</span>
      <span class="ev-cat">${catLabel}</span>
      <span class="ev-title">${safeTitle}</span>
      ${tag}
      ${detailLine}
    </div>
  `;
}

function paintEvents(host, freshIds) {
  if (!host) return;
  if (!_eventsState.items.length) {
    host.innerHTML = `<div class="events-empty">${t('events_empty')}</div>`;
    return;
  }
  const fresh = freshIds || new Set();
  host.innerHTML = _eventsState.items
    .map(ev => eventRowHtml(ev, fresh.has(ev.id)))
    .join('');
  // Footer: "loading more" / "no more"
  const footer = document.getElementById('events-footer');
  if (footer) {
    if (_eventsState.exhausted) {
      footer.style.display = 'block';
      footer.textContent = t('events_no_more') || '— end of stream —';
      footer.classList.add('events-footer-end');
    } else {
      footer.style.display = 'none';
    }
  }
}

// Merge server's top page into our list, keeping any older items we already have.
function mergeTopPage(incoming) {
  const freshIds = new Set();
  if (_eventsState.knownIds.size) {
    for (const ev of incoming) {
      if (!_eventsState.knownIds.has(ev.id)) freshIds.add(ev.id);
    }
  }
  // Build a map by id, prefer incoming for overlapping ids
  const byId = new Map();
  for (const ev of _eventsState.items) byId.set(ev.id, ev);
  for (const ev of incoming) byId.set(ev.id, ev);
  const merged = Array.from(byId.values());
  merged.sort((a, b) => {
    if (a.ts === b.ts) return String(b.id).localeCompare(String(a.id));
    return (a.ts < b.ts) ? 1 : -1;
  });
  _eventsState.items = merged;
  for (const ev of incoming) _eventsState.knownIds.add(ev.id);
  if (_eventsState.knownIds.size > 10000) {
    // Trim known set to currently visible items so it doesn't grow forever
    _eventsState.knownIds = new Set(_eventsState.items.map(e => e.id));
  }
  return freshIds;
}

async function pollEvents() {
  const host = document.getElementById('events-list');
  if (!host) {
    if (_eventsState.timer) { clearInterval(_eventsState.timer); _eventsState.timer = null; }
    return;
  }
  try {
    const resp = await api(`/events?limit=${EVENT_INITIAL}`);
    const incoming = (resp && resp.events) || [];
    if (!incoming.length && !_eventsState.items.length) {
      paintEvents(host, new Set());
      return;
    }
    const freshIds = mergeTopPage(incoming);
    paintEvents(host, freshIds);
    bindScrollLoader();
  } catch (e) {
    console.warn('events poll failed', e);
  }
}

async function loadMoreOlder() {
  if (_eventsState.loadingMore || _eventsState.exhausted) return;
  if (!_eventsState.items.length) return;
  _eventsState.loadingMore = true;
  const footer = document.getElementById('events-footer');
  if (footer) {
    footer.style.display = 'block';
    footer.textContent = t('events_loading_more') || 'Loading older events…';
    footer.classList.remove('events-footer-end');
  }
  try {
    const oldest = _eventsState.items[_eventsState.items.length - 1];
    const beforeTs = encodeURIComponent(oldest.ts);
    const resp = await api(`/events?limit=${EVENT_PAGE}&before_ts=${beforeTs}`);
    const older = (resp && resp.events) || [];
    if (!older.length) {
      _eventsState.exhausted = true;
    } else {
      // Append older items that we don't already have
      const have = new Set(_eventsState.items.map(e => e.id));
      const newOnes = older.filter(e => !have.has(e.id));
      if (!newOnes.length) {
        _eventsState.exhausted = true;
      } else {
        _eventsState.items = _eventsState.items.concat(newOnes);
        for (const ev of newOnes) _eventsState.knownIds.add(ev.id);
        // Re-sort defensively
        _eventsState.items.sort((a, b) => {
          if (a.ts === b.ts) return String(b.id).localeCompare(String(a.id));
          return (a.ts < b.ts) ? 1 : -1;
        });
      }
    }
    const host = document.getElementById('events-list');
    paintEvents(host, new Set());
  } catch (e) {
    console.warn('load older failed', e);
    if (footer) footer.textContent = t('events_load_failed') || 'Failed to load older events';
  } finally {
    _eventsState.loadingMore = false;
  }
}

function bindScrollLoader() {
  if (_eventsState.scrollBound) return;
  const card = document.getElementById('events-card');
  if (!card) return;
  card.addEventListener('scroll', () => {
    const remaining = card.scrollHeight - card.scrollTop - card.clientHeight;
    if (remaining < 80) loadMoreOlder();
  });
  _eventsState.scrollBound = true;
}

function startEventsStream() {
  if (_eventsState.timer) clearInterval(_eventsState.timer);
  _eventsState = {
    items: [], knownIds: new Set(), timer: null,
    loadingMore: false, exhausted: false, scrollBound: false,
  };
  pollEvents();
  _eventsState.timer = setInterval(pollEvents, EVENT_POLL_MS);
}

window.eventsSectionHtml = eventsSectionHtml;
window.startEventsStream = startEventsStream;
