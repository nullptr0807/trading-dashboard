import asyncio
import json
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))


def test_summary_separates_readiness_and_adds_f_and_idx_groups(monkeypatch):
    import api.trade as trade

    trade._API_CACHE.clear()
    rows = [
        {'name': 'A1', 'cash': 110, 'equity': 110, 'timestamp': 'x', 'group': 'A', 'strategy_name': 'a', 'initial_cash': 100, 'status': 'active', 'runtime_status': 'ready', 'runtime_reason': None},
        {'name': 'A2', 'cash': 100, 'equity': 100, 'timestamp': 'x', 'group': 'A', 'strategy_name': 'blocked', 'initial_cash': 100, 'status': 'active', 'runtime_status': 'blocked', 'runtime_reason': 'missing factor'},
        {'name': 'A3', 'cash': 90, 'equity': 90, 'timestamp': 'x', 'group': 'A', 'strategy_name': 'old', 'initial_cash': 100, 'status': 'retired', 'runtime_status': 'ready', 'runtime_reason': None},
        {'name': 'F1', 'cash': 105, 'equity': 105, 'timestamp': 'x', 'group': 'F', 'strategy_name': 'gp', 'initial_cash': 100, 'status': 'active', 'runtime_status': 'ready', 'runtime_reason': None},
        {'name': 'IDX1', 'cash': 102, 'equity': 102, 'timestamp': 'x', 'group': 'IDX', 'strategy_name': 'bench', 'initial_cash': 100, 'status': 'active', 'runtime_status': 'ready', 'runtime_reason': None},
    ]

    async def fake_all(query, params=()):
        q = ' '.join(query.split())
        if 'LEFT JOIN accounts' in q:
            return [dict(r) for r in rows]
        return []

    monkeypatch.setattr(trade, 'fetch_all', fake_all)
    payload = asyncio.run(trade.summary('US'))
    assert payload['group_A']['count'] == 3
    assert payload['group_A']['active_ready_count'] == 1
    assert payload['group_A']['active_nontradeable_count'] == 1
    assert payload['group_A']['retired_count'] == 1
    assert payload['group_F']['count'] == 1
    assert payload['group_IDX']['count'] == 1


def test_trade_queries_are_market_scoped_and_invalid_account_stops_early(monkeypatch):
    import api.trade as trade

    queries = []

    async def fake_all(query, params=()):
        queries.append((' '.join(query.split()), dict(params)))
        return []

    monkeypatch.setattr(trade, 'fetch_all', fake_all)
    sync_calls = []
    monkeypatch.setattr(trade, '_account_detail_sync', lambda account, market: sync_calls.append((account, market)))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(trade.account_detail('SAME-ID', 'CN'))
    assert exc.value.status_code == 404
    assert sync_calls == [('SAME-ID', 'CN')]

    queries.clear()
    asyncio.run(trade.recent_trades(20, 'CN'))
    q = queries[0][0]
    assert 'FROM trades' in q and 'market = :market' in q


def test_equity_curve_first_trade_is_explicitly_market_scoped(monkeypatch):
    import api.trade as trade

    trade._API_CACHE.clear()
    seen = []

    async def fake_all(query, params=()):
        return []

    async def fake_one(query, params=()):
        seen.append(' '.join(query.split()))
        return None

    async def fake_equity(*args, **kwargs):
        return []

    monkeypatch.setattr(trade, 'fetch_all', fake_all)
    monkeypatch.setattr(trade, 'fetch_one', fake_one)
    monkeypatch.setattr(trade, '_fetch_account_equity_rows', fake_equity)
    asyncio.run(trade.equity_curves('CN'))
    first_trade = next(q for q in seen if 'MIN(timestamp)' in q)
    assert 'market = :market' in first_trade


def test_account_detail_uses_market_for_state_first_trade_and_same_id(monkeypatch):
    import api.trade as trade

    seen = []
    def fake_sync(account, market):
        seen.append((account, market))
        return {
            'meta': {'account_id': account, 'market': market, 'initial_cash': 100},
            'state': None, 'positions': [], 'trades': [], 'equity': [],
            'equity_source_points': 0, 'snapshots': [], 'anchor_ts': None,
            'trade_total': 0, 'trade_stats': {'total': 0, 'buys': 0, 'sells': 0},
            'trade_markers': [], 'trade_marker_source_points': 0,
        }
    monkeypatch.setattr(trade, '_account_detail_sync', fake_sync)
    payload = asyncio.run(trade.account_detail('SAME-ID', 'CN'))
    assert payload['market'] == 'CN'
    assert seen == [('SAME-ID', 'CN')]


def test_account_detail_payload_is_bounded_and_preserves_endpoints(monkeypatch):
    import api.trade as trade

    equity_rows = [{'equity': float(i), 'timestamp': f'2026-01-{1 + i // 100:02d}T{i % 100:02d}:00:00'} for i in range(5000)]
    sampled = trade._downsample_endpoints(equity_rows, trade.ACCOUNT_EQUITY_MAX_POINTS)
    def fake_sync(account, market):
        return {
            'meta': {'account_id': account, 'market': market, 'initial_cash': 100},
            'state': None, 'positions': [], 'trades': [], 'equity': sampled,
            'equity_source_points': len(equity_rows), 'snapshots': [], 'anchor_ts': None,
            'trade_total': 0, 'trade_stats': {'total': 0, 'buys': 0, 'sells': 0},
            'trade_markers': [], 'trade_marker_source_points': 0,
        }

    async def no_benchmark(*args, **kwargs):
        return []

    monkeypatch.setattr(trade, '_account_detail_sync', fake_sync)
    monkeypatch.setattr(trade, 'rebased_curve', no_benchmark)
    payload = asyncio.run(trade.account_detail('A1', 'US'))
    curve = payload['equity_curve']
    assert len(curve) <= trade.ACCOUNT_EQUITY_MAX_POINTS
    assert curve[0] == equity_rows[0]
    assert curve[-1] == equity_rows[-1]
    assert payload['limits']['equity_points'] == trade.ACCOUNT_EQUITY_MAX_POINTS
    assert payload['limits']['snapshot_timestamps'] == trade.ACCOUNT_SNAPSHOT_MAX


def test_factor_catalog_runs_sql_in_worker_and_uses_ttl_cache(monkeypatch):
    import api.factor_lab as lab

    lab._CATALOG_CACHE.clear()
    calls = []

    def fake_sync(market):
        calls.append(('sync', market))
        return {'market': market, 'factors': [], 'account_composites': [], 'defaults': {}}

    real_to_thread = asyncio.to_thread

    async def tracking_to_thread(fn, *args, **kwargs):
        calls.append(('thread', args[0]))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(lab, '_factor_lab_catalog_sync', fake_sync, raising=False)
    monkeypatch.setattr(lab.asyncio, 'to_thread', tracking_to_thread)
    first = asyncio.run(lab.factor_lab_catalog('US'))
    second = asyncio.run(lab.factor_lab_catalog('US'))
    assert first == second
    assert calls.count(('sync', 'US')) == 1
    assert calls.count(('thread', 'US')) == 1


def test_gp_factor_lab_translation_preserves_ma_coordinate_and_pairwise_max():
    import numpy as np
    import pandas as pd
    from api.factor_lab import _feature_to_formula_symbol, _gp_expr_to_runnable_formula
    from core.factor_lab_engine import _compute_formula_matrix

    assert _feature_to_formula_symbol('pv_corr_20') == 'rho_20(ROC_1,(VOLUME/lag(VOLUME,1)-1))'

    cols = ['ma_5', 'ma_10', 'ma_20', 'o_c', 'h_c', 'l_c']
    formula = _gp_expr_to_runnable_formula(
        'max2(max2(inv(X0), X3), X3)', cols
    )
    # GP ma_5 is MA5/close; Factor Lab MA_RATIO_5 is close/MA5. The
    # expression's inv(X0) must therefore become close/MA5, not MA5/close.
    assert formula == 'max2(max2((1/((1/MA_RATIO_5))),(OPEN/CLOSE)),(OPEN/CLOSE))'

    idx = pd.Index(['2026-01-01'])
    columns = ['UP', 'DOWN']
    close = pd.DataFrame([[110.0, 90.0]], index=idx, columns=columns)
    open_ = pd.DataFrame([[100.0, 100.0]], index=idx, columns=columns)
    matrices = {
        'close': close,
        'open': open_,
        'high': pd.DataFrame([[112.0, 102.0]], index=idx, columns=columns),
        'low': pd.DataFrame([[99.0, 88.0]], index=idx, columns=columns),
        'volume': pd.DataFrame([[1.0, 1.0]], index=idx, columns=columns),
    }
    # Direct pairwise max/min are distinct from rolling max/min and must work
    # element-wise for translated gplearn expressions.
    got = _compute_formula_matrix('max2(OPEN,CLOSE)', matrices)
    assert np.allclose(got.to_numpy(), [[110.0, 100.0]])


def test_mobile_css_contains_nav_width_guards_and_cache_bust():
    css = (ROOT / 'static/css/style.css').read_text()
    html = (ROOT / 'static/index.html').read_text()
    assert '@media (max-width: 600px)' in css
    assert 'grid-template-columns: minmax(0, 1fr) auto auto' in css
    assert 'overflow-x: auto' in css
    assert 'max-width: 100%' in css
    assert 'style.css?v=53' in html


def test_account_surfaces_display_ids_without_account_names():
    visible_surfaces = [
        'static/js/components.js', 'static/js/trade.js', 'static/js/backtest.js',
        'static/js/symbols.js', 'static/js/factor_lab.js',
    ]
    for relative in visible_surfaces:
        text = (ROOT / relative).read_text()
        assert 'tStrategy(' not in text, relative
    assert '${esc(x.label || x.account_id)}' not in (ROOT / 'static/js/factor_lab.js').read_text()


def test_overview_equity_chart_has_continuous_mouse_time_zoom():
    js = (ROOT / 'static/js/trade.js').read_text()
    html = (ROOT / 'static/index.html').read_text()
    assert 'function installEquityTimeZoom(chart, container)' in js
    assert "container.addEventListener('wheel'" in js
    assert 'scale.setVisibleLogicalRange' in js
    assert 'event.preventDefault()' in js
    assert "container.addEventListener('dblclick'" in js
    assert "handleScale: { axisPressedMouseMove: true, mouseWheel: false, pinch: true }" in js
    assert 'trade.js?v=27' in html


def test_live_account_uses_sse_incremental_updates_and_cache_bust():
    js = (ROOT / 'static/js/live_account.js').read_text()
    html = (ROOT / 'static/index.html').read_text()
    assert 'const LA_API_BASE' in js
    assert "startsWith('/trading-dashboard/')" in js
    assert 'new EventSource(`${LA_API_BASE}/strategy/stream`)' in js
    assert "stream.addEventListener('snapshot'" in js
    assert 'function laApplySnapshot(root,control)' in js
    assert "set('la-control-wrap',laControlPanel(control,p,st))" in js
    assert "if(signature!==_laChartSignature)" in js
    assert "if(document.hidden)laStopStream()" in js
    assert "window.addEventListener('hashchange'" in js
    assert 'laCaptureView(app)' in js and 'laRestoreView(app,view)' in js
    assert "live_account.js?v=19" in (ROOT / 'static/js/app.js').read_text()


def test_return_histogram_and_opend_connection_panel_are_removed():
    trade_js = (ROOT / 'static/js/trade.js').read_text()
    live_js = (ROOT / 'static/js/live_account.js').read_text()
    assert 'id="pnl-histogram"' not in trade_js
    assert 'renderPnlHistogram(d)' not in trade_js
    assert 'id="la-setup-wrap"' not in live_js
    assert "set('la-setup-wrap'" not in live_js


def test_live_page_uses_top_status_only_and_highlights_real_equity():
    live_js = (ROOT / 'static/js/live_account.js').read_text()
    assert 'id="la-livebar-wrap"' not in live_js
    assert 'id="la-shared-warning"' not in live_js
    assert 'id="la-raw-wrap"' not in live_js
    assert "set('la-livebar-wrap'" not in live_js
    assert "set('la-shared-warning'" not in live_js
    assert "set('la-raw-wrap'" not in live_js
    assert "addLineSeries({color:'#00f5a0',lineWidth:4,lineStyle:0" in live_js
    assert "lineStyle:2,priceLineVisible:false,lastValueVisible:false,title:`PAPER" in live_js


def test_strategy_text_is_html_escaped_in_card(tmp_path):
    script = tmp_path / 'check.js'
    script.write_text(
        "const fs=require('fs'),vm=require('vm');"
        "global.t=(x)=>x;global.tStrategy=(x)=>x;global.formatCurrency=(x)=>String(x);global.formatPercent=(x)=>String(x);"
        "let html='';global.document={createElement:()=>({dataset:{},style:{},set className(v){},set innerHTML(v){html=v},get innerHTML(){return html},querySelector:()=>({addEventListener:()=>{}})})};"
        f"vm.runInThisContext(fs.readFileSync({json.dumps(str(ROOT / 'static/js/components.js'))},'utf8'));"
        "createCard({account_id:'A1',strategy_name:'<img src=x onerror=alert(1)>',equity:1,pnl:0,pnl_pct:0});"
        "if(html.includes('<img')||html.includes('&lt;img')||html.includes('strategy_name'))process.exit(1);"
    )
    import subprocess
    result = subprocess.run(['node', str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
