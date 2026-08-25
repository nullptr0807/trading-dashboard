import asyncio
import sqlite3
from pathlib import Path


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        '''
        CREATE TABLE account_meta(
            account_id TEXT, market TEXT, initial_cash REAL, strategy_name TEXT,
            description TEXT, "group" TEXT, factors TEXT, created_at TEXT,
            status TEXT, retired_at TEXT, retire_reason TEXT
        );
        CREATE TABLE account_state(
            account TEXT, market TEXT, cash REAL, initial_cash REAL, updated_at TEXT
        );
        CREATE TABLE positions(
            account TEXT, market TEXT, ticker TEXT, shares REAL, avg_cost REAL,
            total_cost REAL, current_price REAL, updated_at TEXT
        );
        CREATE TABLE trades(
            id INTEGER PRIMARY KEY, account TEXT, market TEXT, ticker TEXT,
            side TEXT, shares REAL, price REAL, cost REAL, slippage REAL,
            timestamp TEXT
        );
        CREATE TABLE accounts(
            id INTEGER PRIMARY KEY, name TEXT, market TEXT, cash REAL,
            equity REAL, timestamp TEXT
        );
        CREATE TABLE positions_history(
            id INTEGER PRIMARY KEY, account TEXT, market TEXT, ticker TEXT,
            shares REAL, avg_cost REAL, market_price REAL, market_value REAL,
            unrealized_pnl REAL, timestamp TEXT
        );
        '''
    )
    con.execute(
        "INSERT INTO account_meta VALUES('A1','US',10000,'s','','A','','2026-01-01','active',NULL,NULL)"
    )
    con.execute("INSERT INTO account_state VALUES('A1','US',1000,10000,'2026-01-01')")
    equity = []
    for i in range(1500):
        ts = f'2026-01-{1 + i // 100:02d}T{i % 100:02d}:00:00'
        equity.append((i + 1, 'A1', 'US', 500.0, 10000.0 + i, ts))
    con.executemany('INSERT INTO accounts VALUES(?,?,?,?,?,?)', equity)
    # The latest 240 snapshot timestamps deliberately include points that the
    # 1,200-point chart downsampler does not retain.
    snapshots = []
    for i, row in enumerate(equity[-240:], 1):
        snapshots.append((i, 'A1', 'US', f'T{i}', 1.0, 100.0, 100.0, 100.0, 0.0, row[-1]))
    con.executemany('INSERT INTO positions_history VALUES(?,?,?,?,?,?,?,?,?,?)', snapshots)
    trades = []
    for i in range(1, 451):
        trades.append((i, 'A1', 'US', 'SPY', 'buy' if i % 2 else 'sell', 1, 100, 100, 0,
                       f'2026-02-{1 + i // 100:02d}T{i % 100:02d}:00:00'))
    con.executemany('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?)', trades)
    con.commit()
    con.close()


def test_all_240_snapshots_keep_equity_and_cash_after_curve_downsampling(tmp_path, monkeypatch):
    import api.trade as trade

    db = tmp_path / 'detail.db'
    _make_db(db)
    monkeypatch.setattr(trade, 'DB_PATH', db)
    detail = trade._account_detail_sync('A1', 'US')
    assert detail is not None
    assert len(detail['equity']) == trade.ACCOUNT_EQUITY_MAX_POINTS
    assert len(detail['snapshots']) == 240
    assert sum(s['equity'] is not None for s in detail['snapshots']) == 240
    assert sum(s['cash'] is not None for s in detail['snapshots']) == 240
    assert all(s['cash'] == s['equity'] - 100.0 for s in detail['snapshots'])


def test_aggregated_trade_marker_uses_share_weighted_price(tmp_path, monkeypatch):
    import api.trade as trade

    db = tmp_path / 'detail.db'
    _make_db(db)
    with sqlite3.connect(db) as con:
        con.executemany(
            'INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?)',
            [
                (451, 'A1', 'US', 'SPY', 'buy', 100, 100, 0, 0,
                 '2026-03-01T12:00:00'),
                (452, 'A1', 'US', 'SPY', 'buy', 1, 10, 0, 0,
                 '2026-03-01T12:00:00'),
            ],
        )
    monkeypatch.setattr(trade, 'DB_PATH', db)
    detail = trade._account_detail_sync('A1', 'US')
    assert detail is not None
    marker = next(m for m in detail['trade_markers'] if m['timestamp'] == '2026-03-01T12:00:00')
    assert marker['shares'] == 101
    assert marker['price'] == (100 * 100 + 1 * 10) / 101


def test_trade_totals_are_full_and_cursor_pages_cover_every_trade(tmp_path, monkeypatch):
    import api.trade as trade

    db = tmp_path / 'detail.db'
    _make_db(db)
    monkeypatch.setattr(trade, 'DB_PATH', db)
    detail = trade._account_detail_sync('A1', 'US')
    assert detail is not None
    assert detail['trade_total'] == 450
    assert detail['trade_stats'] == {'total': 450, 'buys': 225, 'sells': 225}
    assert len(detail['trades']) == trade.ACCOUNT_TRADES_MAX == 200

    async def no_benchmark(*args, **kwargs):
        return []
    monkeypatch.setattr(trade, 'rebased_curve', no_benchmark)
    response = asyncio.run(trade.account_detail('A1', 'US'))
    assert response['trade_total'] == 450
    assert response['trades_truncated'] is True
    assert response['trades_next_cursor']
    assert response['trade_stats'] == {'total': 450, 'buys': 225, 'sells': 225}

    cursor = response['trades_next_cursor']
    ids = {row['id'] for row in response['trades']}
    while cursor:
        page = asyncio.run(trade.account_trades('A1', 'US', 73, cursor))
        ids.update(row['id'] for row in page['trades'])
        cursor = page['next_cursor']
    assert ids == set(range(1, 451))


def test_account_detail_heavy_loader_runs_in_worker(monkeypatch):
    import api.trade as trade

    calls = []
    payload = {
        'meta': {'account_id': 'A1', 'market': 'US', 'initial_cash': 100},
        'state': None, 'positions': [], 'trades': [], 'equity': [],
        'equity_source_points': 0, 'snapshots': [], 'anchor_ts': None,
        'trade_total': 0, 'trade_stats': {'total': 0, 'buys': 0, 'sells': 0},
        'trade_markers': [], 'trade_marker_source_points': 0,
    }

    async def fake_to_thread(fn, *args):
        calls.append((fn, args))
        return payload

    monkeypatch.setattr(trade.asyncio, 'to_thread', fake_to_thread)
    result = asyncio.run(trade.account_detail('A1', 'US'))
    assert result['trade_total'] == 0
    assert calls == [(trade._account_detail_sync, ('A1', 'US'))]
