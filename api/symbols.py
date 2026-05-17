"""Symbol-centric aggregation endpoints.

Surface the trading universe from the perspective of the *ticker*: which
accounts have traded it, how each fared (realized + unrealized PnL), and the
underlying price curve so the dashboard can overlay every account's
buy/sell points on a single chart.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from collections import defaultdict, deque
from fastapi import APIRouter, HTTPException, Query

from core.db import fetch_all, fetch_one
from api.trade import _cn_ticker_names, _validate_market

router = APIRouter(prefix='/api/symbols', tags=['symbols'])


# ----------------------------- profile cache --------------------------------
_PROFILE_CACHE: dict[str, tuple[float, dict]] = {}
_PROFILE_TTL = 12 * 3600

# Persistent translation cache so we don't re-hit Google Translate.
_TRANS_DIR = Path('/home/gexin/trading-dashboard/data/translations')
_TRANS_DIR.mkdir(parents=True, exist_ok=True)
_TRANS_FILE = _TRANS_DIR / 'symbol_zh.json'
try:
    _TRANS: dict = json.loads(_TRANS_FILE.read_text()) if _TRANS_FILE.exists() else {}
except Exception:
    _TRANS = {}


def _save_trans():
    try:
        _TRANS_FILE.write_text(json.dumps(_TRANS, ensure_ascii=False, indent=0))
    except Exception:
        pass


def _translate_zh(text: str) -> str | None:
    """Translate English → Simplified Chinese via free Google Translate.
    Returns None on failure (caller falls back to English).
    """
    if not text:
        return None
    key = text.strip()
    if key in _TRANS:
        return _TRANS[key]
    try:
        from deep_translator import GoogleTranslator
        zh = GoogleTranslator(source='en', target='zh-CN').translate(key)
        if zh:
            _TRANS[key] = zh
            _save_trans()
        return zh
    except Exception:
        return None


def _yf_profile(ticker: str) -> dict:
    cached = _PROFILE_CACHE.get(ticker)
    if cached and (time.time() - cached[0]) < _PROFILE_TTL:
        return cached[1]
    out: dict = {'ticker': ticker, 'name': None, 'name_zh': None,
                 'summary': None, 'summary_zh': None,
                 'sector': None, 'sector_zh': None,
                 'industry': None, 'industry_zh': None,
                 'website': None, 'next_earnings': None, 'source': None}
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        info = {}
        try:
            info = tk.info or {}
        except Exception:
            info = {}
        out['name'] = info.get('longName') or info.get('shortName')
        summary = info.get('longBusinessSummary') or ''
        if summary:
            parts = re.split(r'(?<=[.!?])\s+', summary.strip())
            out['summary'] = ' '.join(parts[:3]).strip()
        out['sector'] = info.get('sector')
        out['industry'] = info.get('industry')
        out['website'] = info.get('website')
        try:
            cal = tk.calendar
            if isinstance(cal, dict):
                ed = cal.get('Earnings Date')
                if isinstance(ed, list) and ed:
                    out['next_earnings'] = str(ed[0])
                elif ed:
                    out['next_earnings'] = str(ed)
        except Exception:
            pass
        if not out['next_earnings']:
            ts = info.get('earningsTimestamp') or info.get('earningsTimestampStart')
            if ts:
                import datetime as _dt
                try:
                    out['next_earnings'] = _dt.datetime.utcfromtimestamp(int(ts)).strftime('%Y-%m-%d')
                except Exception:
                    pass
        # Chinese translations (cached on disk)
        if out['name']:
            out['name_zh'] = _translate_zh(out['name'])
        if out['summary']:
            out['summary_zh'] = _translate_zh(out['summary'])
        if out['sector']:
            out['sector_zh'] = _translate_zh(out['sector'])
        if out['industry']:
            out['industry_zh'] = _translate_zh(out['industry'])
        out['source'] = 'yfinance'
    except Exception as e:
        out['error'] = str(e)
    _PROFILE_CACHE[ticker] = (time.time(), out)
    return out


@router.get('/{ticker}/profile')
async def symbol_profile(ticker: str, market: str = Query('US')):
    _validate_market(market)
    if not re.fullmatch(r'[A-Za-z0-9_.\-]+', ticker):
        raise HTTPException(400, 'invalid ticker')
    return _yf_profile(ticker)


def _fifo_realized(trades: list[dict]) -> tuple[float, float, float]:
    """Walk a single account's trades on a single ticker (time-sorted) and
    return (realized_pnl, remaining_shares, remaining_avg_cost).

    Trade fees (`cost` column) are subtracted from realized PnL on every
    fill so the number agrees with the per-account ledger elsewhere in the
    dashboard.
    """
    lots: deque[list[float]] = deque()  # [shares, unit_cost]
    realized = 0.0
    for tr in trades:
        side = (tr.get('side') or '').lower()
        sh = float(tr.get('shares') or 0)
        px = float(tr.get('price') or 0)
        fee = float(tr.get('cost') or 0)
        if sh <= 0:
            continue
        if side == 'buy':
            lots.append([sh, px])
            realized -= fee
        elif side == 'sell':
            remaining = sh
            while remaining > 1e-9 and lots:
                lot = lots[0]
                take = min(lot[0], remaining)
                realized += (px - lot[1]) * take
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    lots.popleft()
            realized -= fee
    rem_sh = sum(l[0] for l in lots)
    rem_cost = (sum(l[0] * l[1] for l in lots) / rem_sh) if rem_sh > 1e-9 else 0.0
    return realized, rem_sh, rem_cost


@router.get('')
async def list_symbols(market: str = Query('US')):
    """Return one row per ticker traded in this market, with aggregates."""
    market = _validate_market(market)
    rows = await fetch_all(
        '''
        SELECT t.ticker,
               COUNT(DISTINCT t.account) AS accounts_count,
               COUNT(*)                  AS trade_count,
               MAX(t.timestamp)          AS last_trade_ts,
               MIN(t.timestamp)          AS first_trade_ts
        FROM trades t
        JOIN account_meta m ON m.account_id = t.account
        WHERE m.market = :market
        GROUP BY t.ticker
        ORDER BY accounts_count DESC, trade_count DESC
        ''',
        {'market': market},
    )

    # Realized PnL per ticker (FIFO across each account, summed).
    detail_rows = await fetch_all(
        '''
        SELECT t.account, t.ticker, t.side, t.shares, t.price, t.cost, t.timestamp
        FROM trades t
        JOIN account_meta m ON m.account_id = t.account
        WHERE m.market = :market
        ORDER BY t.ticker, t.account, t.timestamp ASC
        ''',
        {'market': market},
    )
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in detail_rows:
        by_pair[(r['ticker'], r['account'])].append(dict(r))
    realized_by_ticker: dict[str, float] = defaultdict(float)
    for (tk, _acc), trs in by_pair.items():
        rp, _, _ = _fifo_realized(trs)
        realized_by_ticker[tk] += rp

    names = _cn_ticker_names() if market == 'CN' else {}
    out = []
    for r in rows:
        tk = r['ticker']
        nm = names.get(tk) or {}
        out.append({
            'ticker': tk,
            'ticker_name_cn': nm.get('cn'),
            'ticker_name_en': nm.get('en'),
            'accounts_count': r['accounts_count'],
            'trade_count': r['trade_count'],
            'first_trade_ts': r['first_trade_ts'],
            'last_trade_ts': r['last_trade_ts'],
            'realized_pnl': round(realized_by_ticker.get(tk, 0.0), 2),
        })
    return {'market': market, 'symbols': out}


@router.get('/{ticker}')
async def symbol_detail(ticker: str, market: str = Query('US')):
    """Per-ticker drill-down: price curve + every account that traded it."""
    market = _validate_market(market)
    if not ticker or '/' in ticker or '..' in ticker:
        raise HTTPException(status_code=400, detail='invalid ticker')

    # Accounts who traded this ticker in this market.
    trade_rows = await fetch_all(
        '''
        SELECT t.id, t.account, t.ticker, t.side, t.shares, t.price, t.cost,
               t.slippage, t.timestamp,
               m.strategy_name, m."group" AS group_name, m.status
        FROM trades t
        JOIN account_meta m ON m.account_id = t.account
        WHERE t.ticker = :tk AND m.market = :market
        ORDER BY t.account, t.timestamp ASC
        ''',
        {'tk': ticker, 'market': market},
    )
    if not trade_rows:
        raise HTTPException(status_code=404, detail=f"no trades for {ticker} in {market}")

    # Latest close from prices table (1d) — used to mark unrealized PnL.
    last_px_row = await fetch_one(
        "SELECT close, datetime FROM prices "
        "WHERE ticker = :tk AND interval = '1d' "
        "ORDER BY datetime DESC LIMIT 1",
        {'tk': ticker},
    )
    last_close = float(last_px_row['close']) if last_px_row and last_px_row['close'] else 0.0

    # Live position rows for this ticker (cross-check + benefit from system's
    # avg_cost calculation). We still derive remaining shares from FIFO so the
    # numbers stay self-consistent on the dashboard side.
    pos_rows = await fetch_all(
        'SELECT account, shares, avg_cost, current_price '
        'FROM positions WHERE ticker = :tk AND market = :market',
        {'tk': ticker, 'market': market},
    )
    pos_by_acc = {r['account']: dict(r) for r in pos_rows}

    # Group trades by account.
    by_acc: dict[str, list[dict]] = defaultdict(list)
    meta_by_acc: dict[str, dict] = {}
    for r in trade_rows:
        d = dict(r)
        by_acc[d['account']].append(d)
        if d['account'] not in meta_by_acc:
            meta_by_acc[d['account']] = {
                'strategy_name': d.get('strategy_name'),
                'group': d.get('group_name'),
                'status': d.get('status'),
            }

    accounts = []
    for acc, trs in sorted(by_acc.items()):
        realized, rem_sh, rem_cost = _fifo_realized(trs)
        # Prefer the live `positions` row when it exists (matches system view).
        live = pos_by_acc.get(acc)
        if live and live.get('shares') is not None:
            rem_sh = float(live['shares'])
            rem_cost = float(live.get('avg_cost') or rem_cost)
            cur_price = float(live.get('current_price') or last_close)
        else:
            cur_price = last_close
        unrealized = (cur_price - rem_cost) * rem_sh if rem_sh > 1e-9 else 0.0
        total_pnl = realized + unrealized
        # cost basis = sum of buy notionals (for return % denominator)
        gross_cost = sum(
            float(tr.get('shares') or 0) * float(tr.get('price') or 0)
            for tr in trs if (tr.get('side') or '').lower() == 'buy'
        )
        ret_pct = (total_pnl / gross_cost * 100) if gross_cost > 0 else 0.0

        # Slim trades payload for the chart markers + table.
        slim_trades = [{
            'id': tr['id'],
            'timestamp': tr['timestamp'],
            'side': tr['side'],
            'shares': tr['shares'],
            'price': tr['price'],
            'cost': tr['cost'],
        } for tr in trs]

        accounts.append({
            'account': acc,
            'group': meta_by_acc[acc]['group'],
            'strategy_name': meta_by_acc[acc]['strategy_name'],
            'status': meta_by_acc[acc]['status'],
            'trade_count': len(trs),
            'realized_pnl': round(realized, 2),
            'remaining_shares': round(rem_sh, 4),
            'remaining_avg_cost': round(rem_cost, 4),
            'current_price': round(cur_price, 4),
            'unrealized_pnl': round(unrealized, 2),
            'total_pnl': round(total_pnl, 2),
            'gross_cost_basis': round(gross_cost, 2),
            'return_pct': round(ret_pct, 2),
            'trades': slim_trades,
        })

    # Sort by total_pnl desc.
    accounts.sort(key=lambda a: a['total_pnl'], reverse=True)

    # Price curve — clip to a window around the trading activity so the chart
    # zooms to relevant range. Pull at most ~600 daily bars (~2.5y) ending at
    # latest price.
    first_trade_ts = min(tr['timestamp'] for tr in trade_rows)
    px_rows = await fetch_all(
        '''
        SELECT datetime AS timestamp, close
        FROM prices
        WHERE ticker = :tk AND interval = '1d'
          AND datetime >= date(:start, '-30 days')
        ORDER BY datetime ASC
        ''',
        {'tk': ticker, 'start': first_trade_ts[:10]},
    )
    price_curve = [
        {'timestamp': r['timestamp'], 'close': r['close']}
        for r in px_rows if r['close'] is not None
    ]

    names = _cn_ticker_names() if market == 'CN' else {}
    nm = names.get(ticker) or {}

    return {
        'market': market,
        'ticker': ticker,
        'ticker_name_cn': nm.get('cn'),
        'ticker_name_en': nm.get('en'),
        'last_close': last_close,
        'last_close_ts': last_px_row['datetime'] if last_px_row else None,
        'price_curve': price_curve,
        'accounts': accounts,
        'total_accounts': len(accounts),
        'total_realized_pnl': round(sum(a['realized_pnl'] for a in accounts), 2),
        'total_unrealized_pnl': round(sum(a['unrealized_pnl'] for a in accounts), 2),
        'total_pnl': round(sum(a['total_pnl'] for a in accounts), 2),
    }
