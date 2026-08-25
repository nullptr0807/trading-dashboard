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

    async def fake_one(query, params=()):
        queries.append((' '.join(query.split()), dict(params)))
        if 'FROM account_meta' in query:
            return None
        return None

    monkeypatch.setattr(trade, 'fetch_all', fake_all)
    monkeypatch.setattr(trade, 'fetch_one', fake_one)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(trade.account_detail('SAME-ID', 'CN'))
    assert exc.value.status_code == 404
    assert len(queries) == 1

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

    async def fake_one(query, params=()):
        q = ' '.join(query.split())
        seen.append((q, dict(params)))
        if 'FROM account_meta' in q:
            return {'account_id': 'SAME-ID', 'market': params['m'], 'initial_cash': 100}
        return None

    async def fake_all(query, params=()):
        seen.append((' '.join(query.split()), dict(params)))
        return []

    monkeypatch.setattr(trade, 'fetch_one', fake_one)
    monkeypatch.setattr(trade, 'fetch_all', fake_all)
    payload = asyncio.run(trade.account_detail('SAME-ID', 'CN'))
    assert payload['market'] == 'CN'
    relevant = [q for q, _ in seen if 'account_state' in q or 'MIN(timestamp)' in q]
    assert relevant
    assert all('market = :m' in q for q in relevant)
    assert all(params.get('m') == 'CN' for q, params in seen if q in relevant)


def test_account_detail_payload_is_bounded_and_preserves_endpoints(monkeypatch):
    import api.trade as trade

    seen = []
    equity_rows = [{'equity': float(i), 'timestamp': f'2026-01-{1 + i // 100:02d}T{i % 100:02d}:00:00'} for i in range(5000)]

    async def fake_one(query, params=()):
        q = ' '.join(query.split())
        if 'FROM account_meta' in q:
            return {'account_id': 'A1', 'market': 'US', 'initial_cash': 100}
        if 'MIN(timestamp)' in q:
            return {'ts': equity_rows[0]['timestamp']}
        return None

    async def fake_all(query, params=()):
        q = ' '.join(query.split())
        seen.append((q, dict(params)))
        if 'SELECT equity, timestamp FROM accounts' in q:
            return equity_rows
        if 'FROM positions_history' in q:
            assert params['snapshot_limit'] <= trade.ACCOUNT_SNAPSHOT_MAX
            return []
        return []

    async def no_benchmark(*args, **kwargs):
        return []

    monkeypatch.setattr(trade, 'fetch_one', fake_one)
    monkeypatch.setattr(trade, 'fetch_all', fake_all)
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


def test_mobile_css_contains_nav_width_guards_and_cache_bust():
    css = (ROOT / 'static/css/style.css').read_text()
    html = (ROOT / 'static/index.html').read_text()
    assert '@media (max-width: 600px)' in css
    assert 'grid-template-columns: minmax(0, 1fr) auto auto' in css
    assert 'overflow-x: auto' in css
    assert 'max-width: 100%' in css
    assert 'style.css?v=44' in html


def test_strategy_text_is_html_escaped_in_card(tmp_path):
    script = tmp_path / 'check.js'
    script.write_text(
        "const fs=require('fs'),vm=require('vm');"
        "global.t=(x)=>x;global.tStrategy=(x)=>x;global.formatCurrency=(x)=>String(x);global.formatPercent=(x)=>String(x);"
        "let html='';global.document={createElement:()=>({dataset:{},style:{},set className(v){},set innerHTML(v){html=v},get innerHTML(){return html},querySelector:()=>({addEventListener:()=>{}})})};"
        f"vm.runInThisContext(fs.readFileSync({json.dumps(str(ROOT / 'static/js/components.js'))},'utf8'));"
        "createCard({account_id:'A1',strategy_name:'<img src=x onerror=alert(1)>',equity:1,pnl:0,pnl_pct:0});"
        "if(html.includes('<img'))process.exit(1);if(!html.includes('&lt;img'))process.exit(2);"
    )
    import subprocess
    result = subprocess.run(['node', str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
