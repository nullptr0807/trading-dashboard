"""Small write helpers for dashboard startup maintenance.

Normal API reads go through core.db with PRAGMA query_only=ON.  This module is
only for idempotent local maintenance such as creating indexes that make those
read queries fast.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / 'quant-trading' / 'data' / 'trading.db'


def ensure_read_indexes() -> None:
    """Create non-destructive indexes used by dashboard read-heavy endpoints."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_accounts_market_name_ts '
            'ON accounts(market, name, timestamp)'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_accounts_market_name_hour_ts '
            'ON accounts(market, name, substr(timestamp,1,13), timestamp)'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_accounts_market_hour_name_ts '
            'ON accounts(market, substr(timestamp,1,13), name, timestamp)'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_accounts_market_ts '
            'ON accounts(market, timestamp)'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_poshist_market_acct_ts '
            'ON positions_history(market, account, timestamp)'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_trades_market_account_ts '
            'ON trades(market, account, timestamp)'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_fv_group_name_date_ticker '
            'ON factor_values(factor_group, factor_name, date, ticker)'
        )
        conn.commit()
    finally:
        conn.close()
