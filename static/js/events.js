// events.js — Live system event stream (poll-based, newest on top, max 50)
// Categories: system | data | factor | rebalance | trade
// Color: system events use blue dot. Higher-priority categories will be added later.

const EVENT_MAX = 50;
const EVENT_POLL_MS = 4000;

const EVENT_STYLE = {
  system:    { dot: 'var(--accent-blue)', label: 'events_cat_system' },
  data:      { dot: 'var(--accent-blue)', label: 'events_cat_data' },
  factor:    { dot: 'var(--accent-blue)', label: 'events_cat_factor' },
  rebalance: { dot: 'var(--accent-blue)', label: 'events_cat_rebalance' },
  trade:     { dot: 'var(--accent-blue)', label: 'events_cat_trade' },
  lifecycle: { dot: '#ffb74d',            label: 'events_cat_lifecycle' },
  inception: { dot: '#4caf50',            label: 'events_cat_inception' },
};

let _eventsState = {
  items: [],          // newest first
  knownIds: new Set(), // ids we've already shown (for fresh-highlight)
  timer: null,
  mountedHost: null,
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
      <div class="glass-card events-card">
        <div class="events-list" id="events-list">
          <div class="events-empty">${t('events_loading')}</div>
        </div>
      </div>
    </div>
  `;
}

function fmtEventTime(iso) {
  // Show local HH:MM:SS, plus MM-DD if not today
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
  // Escape title to prevent XSS (titles come from server but are user-data adjacent)
  const escape = s => String(s || '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
  const safeTitle = escape(ev.title);
  // Lifecycle events (e.g. retire) carry detail JSON like {"reason":"...","retired_at":"..."}
  // — surface the human-readable reason as a sub-line so the user can see WHY
  // an account was retired without clicking through.
  let detailLine = '';
  if (ev.detail) {
    try {
      const d = JSON.parse(ev.detail);
      if (d && d.reason) detailLine = `<span class="ev-detail">↳ ${escape(d.reason)}</span>`;
    } catch { /* not JSON, ignore */ }
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
}

async function pollEvents() {
  const host = document.getElementById('events-list');
  if (!host) {
    // Section was removed (e.g. navigated away) — stop the timer.
    if (_eventsState.timer) { clearInterval(_eventsState.timer); _eventsState.timer = null; }
    return;
  }
  try {
    // Full refresh: API orders by ts DESC, so we always get the temporally newest N.
    // We avoid `after_id` incremental polling because backfilled events break the
    // assumption that id is monotonic in time (a freshly inserted row may have an
    // older ts than rows already shown).
    const resp = await api(`/events?limit=${EVENT_MAX}`);
    const incoming = (resp && resp.events) || [];
    if (!incoming.length) {
      if (!_eventsState.items.length) paintEvents(host, new Set());
      return;
    }

    const freshIds = new Set();
    if (_eventsState.knownIds.size) {
      for (const ev of incoming) {
        if (!_eventsState.knownIds.has(ev.id)) freshIds.add(ev.id);
      }
    }
    _eventsState.items = incoming;
    for (const ev of incoming) _eventsState.knownIds.add(ev.id);
    // Cap memory of knownIds to avoid unbounded growth
    if (_eventsState.knownIds.size > 5000) {
      _eventsState.knownIds = new Set(incoming.map(e => e.id));
    }
    paintEvents(host, freshIds);
  } catch (e) {
    // Silent — don't spam UI on transient failures
    console.warn('events poll failed', e);
  }
}

function startEventsStream() {
  // Stop any prior timer (route changes)
  if (_eventsState.timer) clearInterval(_eventsState.timer);
  _eventsState = { items: [], knownIds: new Set(), timer: null, mountedHost: null };
  pollEvents();
  _eventsState.timer = setInterval(pollEvents, EVENT_POLL_MS);
}

// Expose for trade.js
window.eventsSectionHtml = eventsSectionHtml;
window.startEventsStream = startEventsStream;
