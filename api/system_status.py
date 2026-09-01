"""Read-only operational status for the trading overview banner."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
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


def _age_seconds(value: Any) -> float:
    if not value:
        return float('inf')
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return float('inf')


def _market_phase(market: str) -> str:
    """Return the current quote session used by the status banner."""
    now = datetime.now(timezone.utc)
    if market == 'CN':
        local = now.astimezone(ZoneInfo('Asia/Shanghai'))
        minute = local.hour * 60 + local.minute
        if local.weekday() < 5 and (570 <= minute < 690 or 780 <= minute < 900):
            return 'rth'
        return 'closed'
    local = now.astimezone(ZoneInfo('America/New_York'))
    minute = local.hour * 60 + local.minute
    if local.weekday() < 5 and 570 <= minute < 960:
        return 'rth'
    if local.weekday() < 5 and 240 <= minute < 1200:
        return 'extended'
    return 'closed'


def _market_freshness_sla(market: str) -> int:
    phase = _market_phase(market)
    if phase == 'rth':
        return 10 * 60
    if phase == 'extended':
        return 30 * 60
    return 96 * 3600


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

        phase = _market_phase(market)
        freshness_sla = _market_freshness_sla(market)
        quote_stale = _age_seconds(health.get('success_at')) > freshness_sla
        quote_unhealthy = health['status'] not in {'ok', 'healthy'}
        if phase in {'extended', 'closed'} and not quote_stale:
            # Sparse pre/post-market prints are expected: many held symbols do
            # not trade in the extended session. Preserve the recorded state as
            # diagnostic evidence, but do not present missing prints as a whole-
            # system incident. The updater heartbeat must still be fresh, and
            # RTH remains strict.
            health['recorded_status'] = health['status']
            health['status'] = phase
            quote_unhealthy = False
        valuation_unhealthy = phase == 'rth' and (
            valuation['complete_accounts'] < valuation['active_accounts']
            or _age_seconds(valuation.get('oldest_complete_at')) > freshness_sla
        )
        degraded = (
            quote_unhealthy
            or quote_stale
            or valuation_unhealthy
            or risk['state'] not in {'ARMED', 'DISARMED'}
            or _age_seconds(risk.get('last_check_at')) > freshness_sla
        )
        return {
            'market': market,
            'market_phase': phase,
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
    return await asyncio.to_thread(_status_sync, _validate_market(market))
