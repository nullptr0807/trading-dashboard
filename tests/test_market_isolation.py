import asyncio


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
