from pathlib import Path

ROOT = Path(__file__).parents[1]


def read(relative):
    return (ROOT / relative).read_text()


def test_trade_first_paint_does_not_eager_load_route_only_bundles():
    html = read('static/index.html')
    for asset in ('katex.min.js', 'auto-render.min.js', 'marked.min.js',
                  'backtest.js', 'factor_lab.js', 'symbols.js', 'live_account.js'):
        assert f'<script src="static/' not in html or asset not in html
    assert 'data-route-src' not in html
    app = read('static/js/app.js')
    assert 'loadRouteAssets' in app
    assert 'encodeURI' in app


def test_router_owns_abort_signal_and_disposes_route_resources():
    app = read('static/js/app.js')
    assert 'new AbortController()' in app
    assert 'registerRouteCleanup' in app
    assert 'disposeActiveRoute' in app
    assert '.abort()' in app
    for relative in ('static/js/trade.js', 'static/js/components.js',
                     'static/js/factor_lab.js', 'static/js/symbols.js',
                     'static/js/live_account.js', 'static/js/events.js'):
        js = read(relative)
        assert 'registerRouteCleanup' in js, relative


def test_home_order_and_incremental_important_events_contract():
    trade = read('static/js/trade.js')
    assert trade.index('id="system-status-banner"') < trade.index('id="equity-chart"')
    assert trade.index('id="equity-chart"') < trade.index('id="accounts-grid"')
    assert trade.index('id="accounts-grid"') < trade.index('eventsSectionHtml')
    events = read('static/js/events.js')
    assert 'EVENT_COLLAPSED_COUNT = 3' in events
    assert 'isImportantEvent' in events
    assert 'insertAdjacentHTML' in events
    assert 'since_id=' in events
    assert 'visibilitychange' in events
    assert 'host.innerHTML = _eventsState.items' not in events


def test_equity_defaults_to_active_group_aggregates_and_o1_crosshair_lookup():
    js = read('static/js/trade.js')
    assert 'aggregateActiveCurves' in js
    assert "new Set(['A', 'B', 'F', 'Q'])" in js
    assert 'showRetired: false' in js
    assert 'seriesNameByRef = new Map()' in js
    assert 'Object.keys(seriesMap).find' not in js
    assert 'equity-curve-controls' in js


def test_live_page_has_one_readiness_truth_and_collapsed_control_center():
    js = read('static/js/live_account.js')
    assert 'status.place_order_ready' in js
    assert 'laPrimaryStatus' in js
    assert '<details class="la-control-center"' in js
    assert 'laConfirmDanger' in js
    assert 'dialog' in js
    assert 'active_holds' in js and 'pending' in js and 'last_sync_at' in js
    assert 'la-btn unfreeze' in js
    assert "status.auto_trading_enabled\n    ? {tone:'live'" not in js


def test_symbols_are_paginated_and_accessible():
    js = read('static/js/symbols.js')
    assert 'PAGE_SIZE = 75' in js
    assert 'aria-sort' in js
    assert 'sym-page-prev' in js and 'sym-page-next' in js
    assert 'aria-expanded' in js
    css = read('static/css/style.css')
    assert '.sym-row-head' in css and 'position: sticky' in css


def test_global_a11y_and_motion_contracts():
    html = read('static/index.html')
    assert '<label for="lang-select"' in html
    assert 'aria-selected="true"' in html
    trade = read('static/js/trade.js')
    assert '<label class="sort-label" for="sort-select">' in trade
    assert 'role="dialog"' in trade and 'aria-modal="true"' in trade
    components = read('static/js/components.js')
    assert '<button class="row-main"' in components
    css = read('static/css/style.css')
    reduced = css[css.index('@media (prefers-reduced-motion: reduce)'):]
    assert 'animation-duration' in reduced and 'transition-duration' in reduced
    assert 'min-height: 44px' in css
