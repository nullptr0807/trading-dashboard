"""Read-only operational status for the trading overview banner."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from api.trade import _validate_market
from core.db import DB_PATH

router = APIRouter(prefix='/api/system-status', tags=['system_status'])


def _json(value: Any) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _status_sync(market: str, db_path: str | Path = DB_PATH) -> dict:
    con = sqlite3.connect(f'file:{Path(db_path)}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        active_ids: list[str] = []
        inactive: list[dict] = []
        if 'account_meta' in tables:
            meta_cols = {r[1] for r in con.execute('PRAGMA table_info(account_meta)')}
            runtime_select = (
                ",COALESCE(runtime_status,'ready') runtime_status,runtime_reason"
                if 'runtime_status' in meta_cols else ", 'ready' runtime_status, NULL runtime_reason"
            )
            meta = con.execute(
                "SELECT account_id,status,retire_reason" + runtime_select + " FROM account_meta "
                "WHERE market=? ORDER BY account_id",
                (market,),
            ).fetchall()
            active_ids = [
                str(r['account_id']) for r in meta
                if str(r['status'] or 'active') == 'active'
            ]
            inactive = [
                {
                    'account_id': str(r['account_id']),
                    'status': str(r['runtime_status'] or r['status'] or 'active'),
                    'reason': r['runtime_reason'] or r['retire_reason'],
                }
                for r in meta
                if str(r['status'] or 'active') not in {'active', 'retired'}
                or (
                    str(r['status'] or 'active') == 'active'
                    and str(r['runtime_status'] or 'ready') != 'ready'
                )
            ]

        valuation = {
            'active_accounts': len(active_ids),
            'complete_accounts': 0,
            'oldest_complete_at': None,
            'newest_complete_at': None,
        }
        if active_ids and 'accounts' in tables:
            marks = ','.join('?' for _ in active_ids)
            rows = con.execute(
                "SELECT m.account_id,(SELECT a.timestamp FROM accounts a "
                "WHERE a.market=m.market AND a.name=m.account_id "
                "ORDER BY a.timestamp DESC LIMIT 1) ts "
                f"FROM account_meta m WHERE m.market=? AND m.account_id IN ({marks})",
                (market, *active_ids),
            ).fetchall()
            timestamps = [str(r['ts']) for r in rows if r['ts']]
            valuation.update({
                'complete_accounts': len(timestamps),
                'oldest_complete_at': min(timestamps) if timestamps else None,
                'newest_complete_at': max(timestamps) if timestamps else None,
            })

        health = {
            'status': 'unknown', 'success_at': None,
            'source_timestamp': None, 'details': {},
        }
        if 'operational_health' in tables:
            row = con.execute(
                "SELECT status,success_at,source_timestamp,details "
                "FROM operational_health WHERE component='update_prices' AND market=?",
                (market,),
            ).fetchone()
            if row:
                health = {
                    'status': str(row['status']),
                    'success_at': row['success_at'],
                    'source_timestamp': row['source_timestamp'],
                    'details': _json(row['details']),
                }

        risk = {'state': 'UNKNOWN', 'drawdown': None, 'last_check_at': None}
        if 'risk_regime' in tables:
            cols = {r[1] for r in con.execute('PRAGMA table_info(risk_regime)')}
            if 'market' in cols:
                row = con.execute(
                    "SELECT state,last_drawdown,last_check_at FROM risk_regime WHERE market=?",
                    (market,),
                ).fetchone()
            else:
                # Legacy singleton is deliberately labelled mixed/unsafe instead
                # of pretending the global state belongs to this market.
                row = con.execute(
                    "SELECT state,last_drawdown,last_check_at FROM risk_regime WHERE id=1"
                ).fetchone()
                if row:
                    risk = {
                        'state': 'LEGACY_MIXED_' + str(row['state']),
                        'drawdown': row['last_drawdown'],
                        'last_check_at': row['last_check_at'],
                    }
                    row = None
            if row:
                risk = {
                    'state': str(row['state']),
                    'drawdown': row['last_drawdown'],
                    'last_check_at': row['last_check_at'],
                }

        degraded = (
            health['status'] not in {'ok', 'healthy'}
            or valuation['complete_accounts'] < valuation['active_accounts']
            or risk['state'].startswith('LEGACY_MIXED_')
            or bool(inactive)
        )
        return {
            'market': market,
            'status': 'degraded' if degraded else 'healthy',
            'quote_health': health,
            'valuation': valuation,
            'risk': risk,
            'non_tradeable_accounts': inactive,
        }
    finally:
        con.close()


@router.get('')
async def system_status(market: str = Query('US')):
    return _status_sync(_validate_market(market))
