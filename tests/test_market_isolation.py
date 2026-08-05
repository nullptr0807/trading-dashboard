import asyncio
import sqlite3


def test_server_startup_does_not_mutate_trading_database():
    from pathlib import Path
    source=(Path(__file__).parents[1]/'server.py').read_text()
    assert 'ensure_read_indexes()' not in source


def test_hourly_curve_uses_bucket_close_not_average(tmp_path, monkeypatch):
    import api.trade as trade
    db=tmp_path/'hourly.db';con=sqlite3.connect(db)
    con.executescript(
        'CREATE TABLE account_meta(account_id TEXT,market TEXT);'
        'CREATE TABLE accounts(name TEXT,market TEXT,equity REAL,cash REAL,timestamp TEXT);'
    )
    con.execute("INSERT INTO account_meta VALUES('A','US')")
    con.executemany(
        "INSERT INTO accounts VALUES('A','US',?,?,?)",
        [(10,1,'2026-08-05T10:01:00'),(1000,1,'2026-08-05T10:02:00'),(11,1,'2026-08-05T10:03:00')],
    )
    con.commit();con.close();monkeypatch.setattr(trade,'DB_PATH',db)
    rows=trade._fetch_account_equity_rows_sync('US')
    assert rows[0][1]==11


def test_accounts_preserves_retired_tombstone_without_snapshot(monkeypatch):
    import api.trade as trade
    trade._API_CACHE.clear()
    async def fake(query, params=()):
        q=' '.join(query.split())
        if 'FROM account_meta m' in q and 'LEFT JOIN accounts' in q:
            return [{
                'name':'C01','cash':None,'equity':None,'timestamp':None,
                'group':'C','strategy_name':'retired','factors':'','status':'retired',
                'initial_cash':10000,'retired_at':'2026-01-01','retire_reason':'done',
                'created_at':'2025-01-01','runtime_status':'ready','runtime_reason':None,
                'runtime_detail':None,'runtime_updated_at':None,
            }]
        return []
    async def no_equity(*args,**kwargs): return []
    monkeypatch.setattr(trade,'fetch_all',fake)
    monkeypatch.setattr(trade,'_fetch_account_equity_rows',no_equity)
    rows=asyncio.run(trade.accounts('US'))
    assert rows[0]['account_id']=='C01'
    assert rows[0]['equity']==10000
    assert rows[0]['status']=='retired'


def test_symbols_queries_bind_trade_and_position_market(monkeypatch):
    import api.symbols as symbols
    queries=[]
    async def fake(query, params=()):
        queries.append(' '.join(query.split()))
        return []
    monkeypatch.setattr(symbols,'fetch_all',fake)
    result=asyncio.run(symbols.list_symbols('US'))
    assert result=={'market':'US','symbols':[]}
    trade_queries=[q for q in queries if 'FROM trades t' in q]
    assert trade_queries
    assert all('m.market = t.market' in q and 't.market = :market' in q for q in trade_queries)


def test_symbol_detail_queries_bind_market(monkeypatch):
    import api.symbols as symbols
    queries=[]
    async def fake_all(query, params=()):
        normalized=' '.join(query.split())
        queries.append(normalized)
        if 'FROM trades t' in normalized:
            return [{
                'id':1,'account':'A01','ticker':'AAPL','side':'buy','shares':1,
                'price':100,'cost':0,'slippage':0,'timestamp':'2026-01-01',
                'strategy_name':'s','group_name':'A','status':'active',
            }]
        return []
    async def fake_one(query, params=()):
        return {'close':100,'datetime':'2026-01-01'}
    monkeypatch.setattr(symbols,'fetch_all',fake_all)
    monkeypatch.setattr(symbols,'fetch_one',fake_one)
    try:
        asyncio.run(symbols.symbol_detail('AAPL','US'))
    except Exception:
        pass
    assert any('m.market = t.market' in q and 't.market = :market' in q for q in queries if 'FROM trades t' in q)
    assert any('m.market = p.market' in q for q in queries if 'FROM positions p' in q)
