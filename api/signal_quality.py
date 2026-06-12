"""
Signal quality / Rank IC diagnostics.

Computes per-account cross-sectional Rank IC and rolling ICIR from the persisted
factor_values table and 1d close prices. The alignment deliberately uses the
tradable future window: signal at date t predicts close[t+1+h] / close[t+1] - 1,
so we do not accidentally score same-close information that was unavailable at
trade time.
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from core.db import DB_PATH, fetch_one
from api.trade import _validate_market

router = APIRouter(prefix='/api/signal-quality', tags=['signal_quality'])

# A single account IC calculation touches ~10^5 rows and is fast enough, but UI
# expands / horizon toggles can repeat it. In-process TTL avoids needless DB work.
_CACHE_TTL = 10 * 60
_CACHE: dict[tuple[str, str, int, int], tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = asyncio.Lock()

_QT_ROOT = os.path.expanduser('~/quant-trading')
if _QT_ROOT not in sys.path:
    sys.path.insert(0, _QT_ROOT)

try:
    from accounts.strategies import STRATEGIES as _A_STRATEGIES
    _A_BY_ID = {s.id: s for s in _A_STRATEGIES}
except Exception:
    _A_BY_ID = {}

try:
    from factors.signal import STRATEGY_FACTORS as _STRATEGY_FACTORS
except Exception:
    _STRATEGY_FACTORS = {}

_ALPHA158_DEFAULT = [
    'KMID', 'KLEN', 'KMID2', 'KUP', 'KUP2', 'KLOW', 'KLOW2', 'KSFT', 'KSFT2',
    'ROC_5', 'ROC_10', 'ROC_20', 'MA_RATIO_5', 'MA_RATIO_10', 'MA_RATIO_20',
    'VMOM_5', 'VMOM_10', 'VMOM_20', 'VSTD_5', 'VSTD_10', 'VSTD_20',
    'STD_5', 'STD_10', 'STD_20', 'BBPOS_5', 'BBPOS_10', 'BBPOS_20',
    'RSV', 'RSI_14', 'BETA_5', 'BETA_10', 'BETA_20',
]


def _round(v: Any, nd: int = 4):
    try:
        if v is None or pd.isna(v) or not math.isfinite(float(v)):
            return None
        return round(float(v), nd)
    except Exception:
        return None


def _empty(account_id: str, market: str, horizon: int, window: int, message: str, *, code: str = 'unsupported', source: dict | None = None):
    return {
        'account_id': account_id,
        'market': market,
        'horizon': horizon,
        'window': window,
        'method': 'rank_ic',
        'supported': False,
        'status': code,
        'message': message,
        'signal_source': source or {},
        'summary': {},
        'series': [],
        'coverage': {},
        'warnings': [message] if message else [],
    }


def _parse_csv_factors(s: str) -> list[str]:
    return [x.strip() for x in (s or '').split(',') if x.strip()]


def _signal_spec(account_id: str, meta: dict) -> dict:
    group = (meta.get('group') or '').upper()
    factors_str = meta.get('factors') or ''
    strategy_name = meta.get('strategy_name') or ''

    if account_id.startswith('IDX') or group == 'IDX':
        return {'supported': False, 'reason': '指数账户没有横截面选股 signal，不能计算 IC。', 'group': group or 'IDX'}

    # CN accounts carry a C prefix: CA01/CB02/CQ03. Base id maps to A/B/Q config.
    base_id = account_id[1:] if account_id.startswith('C') and len(account_id) >= 3 else account_id
    base_group = group

    if group == 'A':
        cfg = _A_BY_ID.get(base_id)
        names = _parse_csv_factors(factors_str)
        strategy_type = getattr(cfg, 'strategy_type', '') if cfg else ''
        # A06 has empty account_meta.factors by design: strategy_type=composite → all Alpha158 factors.
        if not names:
            if cfg and getattr(cfg, 'factor_names', None):
                names = list(cfg.factor_names)
            elif strategy_type and _STRATEGY_FACTORS.get(strategy_type) is not None:
                names = list(_STRATEGY_FACTORS[strategy_type])
            else:
                names = list(_ALPHA158_DEFAULT)
        direction = -1 if strategy_type == 'mean_reversion' else 1
        return {
            'supported': True,
            'group': 'A',
            'base_id': base_id,
            'factor_group': 'alpha158',
            'factor_names': names,
            'aggregation': 'rank_mean_rank',
            'direction': direction,
            'strategy_type': strategy_type or None,
            'strategy_name': strategy_name,
        }

    if group == 'Q':
        q_id = base_id if base_id.startswith('Q') else base_id.lstrip('C')
        fname = f'qlib_{q_id}_score'
        return {
            'supported': True,
            'group': 'Q',
            'base_id': q_id,
            'factor_group': 'qlib',
            'factor_names': [fname],
            'aggregation': 'model_predict_then_rank',
            'direction': 1,
            'strategy_name': strategy_name,
        }

    if group in ('B', 'F'):
        prefix = 'fmgp' if group == 'F' or factors_str.startswith('FMGP') else 'gp'
        return {
            'supported': True,
            'group': group,
            'base_id': base_id,
            'factor_group': f'{prefix}_{account_id}',
            'factor_names': [],  # discover from factor_group
            'aggregation': 'gp_expression_then_rank',
            'direction': 1,
            'strategy_name': strategy_name,
        }

    return {'supported': False, 'reason': f'账户组 {group or "?"} 暂不支持 IC 计算。', 'group': base_group}


def _compute_signal_quality_sync(account_id: str, market: str, horizon: int, window: int) -> dict[str, Any]:
    import sqlite3

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        meta = con.execute(
            'SELECT * FROM account_meta WHERE account_id = ? AND market = ?',
            (account_id, market),
        ).fetchone()
        if not meta:
            raise HTTPException(status_code=404, detail='account not found')
        meta = dict(meta)
        spec = _signal_spec(account_id, meta)
        if not spec.get('supported'):
            return _empty(account_id, market, horizon, window, spec.get('reason') or 'unsupported', source=spec)

        factor_group = spec.get('factor_group')
        factor_names = list(spec.get('factor_names') or [])
        params: list[Any]
        if factor_names:
            placeholders = ','.join(['?'] * len(factor_names))
            q = (
                f'SELECT ticker, date, factor_name, value FROM factor_values '
                f'WHERE factor_group = ? AND factor_name IN ({placeholders}) '
                f'ORDER BY date, ticker, factor_name'
            )
            params = [factor_group, *factor_names]
        else:
            q = (
                'SELECT ticker, date, factor_name, value FROM factor_values '
                'WHERE factor_group = ? ORDER BY date, ticker, factor_name'
            )
            params = [factor_group]

        fv = pd.read_sql_query(q, con, params=params)
        if fv.empty:
            return _empty(
                account_id, market, horizon, window,
                f'暂无 persisted factor_values（factor_group={factor_group}）。',
                code='no_factor_values', source=spec,
            )

        # Discover B/F factor names after reading by group. For A/Q preserve the declared order.
        if not factor_names:
            factor_names = sorted(fv['factor_name'].dropna().unique().tolist())
            spec['factor_names'] = factor_names
        fv = fv.dropna(subset=['value'])
        if fv.empty:
            return _empty(account_id, market, horizon, window, 'factor_values 全为空。', code='no_factor_values', source=spec)

        # factor_values is not market-scoped for shared A/Q columns. Split the
        # universe explicitly; otherwise a CN account accidentally scores US
        # tickers (and vice versa), producing a fake ~1300-stock universe.
        is_cn_ticker = fv['ticker'].astype(str).str.match(r'^\d{6}\.(SH|SZ)$', na=False)
        fv = fv[is_cn_ticker if market == 'CN' else ~is_cn_ticker].copy()
        if fv.empty:
            return _empty(account_id, market, horizon, window, f'factor_values 中没有 {market} 市场 ticker。', code='no_market_rows', source=spec)

        # Composite signal per date × ticker:
        # - each factor: cross-sectional percentile rank per date
        # - equal-weight mean across factors
        # - final cross-sectional rank
        fv['factor_rank'] = fv.groupby(['date', 'factor_name'])['value'].rank(pct=True)
        comp = fv.groupby(['date', 'ticker'])['factor_rank'].mean().reset_index(name='score')
        comp['signal'] = comp.groupby('date')['score'].rank(pct=True)
        if int(spec.get('direction') or 1) < 0:
            comp['signal'] = 1.0 - comp['signal']

        tickers = sorted(comp['ticker'].dropna().unique().tolist())
        if not tickers:
            return _empty(account_id, market, horizon, window, '无可用 ticker。', code='no_tickers', source=spec)
        placeholders = ','.join(['?'] * len(tickers))
        prices = pd.read_sql_query(
            f'SELECT ticker, datetime, close FROM prices '
            f'WHERE interval = ? AND ticker IN ({placeholders}) ORDER BY ticker, datetime',
            con,
            params=['1d', *tickers],
        )
        if prices.empty:
            return _empty(account_id, market, horizon, window, '找不到对应 1d close 价格。', code='no_prices', source=spec)
        prices['date'] = pd.to_datetime(prices['datetime']).dt.strftime('%Y-%m-%d')
        close = prices.pivot_table(index='date', columns='ticker', values='close', aggfunc='last').sort_index()
        # Tradable forward return: signal[t] -> close[t+1+h] / close[t+1] - 1.
        future_ret = close.shift(-(horizon + 1)) / close.shift(-1) - 1
        ret_long = future_ret.stack().rename('future_return').reset_index()
        ret_long.columns = ['date', 'ticker', 'future_return']

        merged = comp[['date', 'ticker', 'signal']].merge(ret_long, on=['date', 'ticker'], how='inner')
        if merged.empty:
            return _empty(
                account_id, market, horizon, window,
                f'价格与因子日期无法对齐，或 horizon={horizon} 后没有未来收益。',
                code='no_overlap', source=spec,
            )

        rows = []
        for dt, g in merged.groupby('date', sort=True):
            g = g.dropna(subset=['signal', 'future_return'])
            n = int(len(g))
            if n < 30:
                continue
            # Spearman IC = Pearson correlation of ranks.
            ic = g['signal'].rank(pct=True).corr(g['future_return'].rank(pct=True))
            if pd.notna(ic) and math.isfinite(float(ic)):
                rows.append({'date': dt, 'ic': float(ic), 'n': n})

        if not rows:
            return _empty(account_id, market, horizon, window, '有效横截面样本少于 30，无法计算 IC。', code='insufficient_data', source=spec)

        ic_df = pd.DataFrame(rows).sort_values('date')
        ic_df['rolling_ic'] = ic_df['ic'].rolling(window, min_periods=max(5, min(window, 10))).mean()
        roll_std = ic_df['ic'].rolling(window, min_periods=max(5, min(window, 10))).std()
        ic_df['rolling_icir'] = ic_df['rolling_ic'] / roll_std.replace(0, np.nan)

        mean_ic = float(ic_df['ic'].mean())
        std_ic = float(ic_df['ic'].std()) if len(ic_df) > 1 else float('nan')
        icir = mean_ic / std_ic if std_ic and math.isfinite(std_ic) and std_ic != 0 else float('nan')
        ann_icir = icir * math.sqrt(252) if math.isfinite(icir) else float('nan')
        latest = ic_df.iloc[-1]
        latest_rolling = ic_df['rolling_icir'].dropna()
        latest_roll_ic = ic_df['rolling_ic'].dropna()

        warnings: list[str] = []
        if len(ic_df) < 50:
            warnings.append(f'有效 IC 样本仅 {len(ic_df)} 天，统计显著性有限。')
        if horizon >= 20 and len(ic_df) < 40:
            warnings.append('长 horizon 会吞掉更多尾部样本；当前窗口更适合看方向，不适合下定论。')
        if int(spec.get('direction') or 1) < 0:
            warnings.append('该账户为均值回归/反转策略，IC 已按真实交易方向取反。')

        series = []
        for r in ic_df.replace({np.nan: None}).to_dict(orient='records'):
            series.append({
                'date': r['date'],
                'ic': _round(r['ic'], 5),
                'rolling_ic': _round(r.get('rolling_ic'), 5),
                'rolling_icir': _round(r.get('rolling_icir'), 4),
                'n': int(r['n']),
            })

        return {
            'account_id': account_id,
            'market': market,
            'horizon': horizon,
            'window': window,
            'method': 'rank_ic',
            'supported': True,
            'status': 'ok',
            'signal_source': {
                'group': spec.get('group'),
                'base_id': spec.get('base_id'),
                'strategy_name': spec.get('strategy_name'),
                'strategy_type': spec.get('strategy_type'),
                'factor_group': spec.get('factor_group'),
                'factors': factor_names,
                'aggregation': spec.get('aggregation'),
                'direction': spec.get('direction', 1),
            },
            'summary': {
                'mean_ic': _round(mean_ic, 5),
                'std_ic': _round(std_ic, 5),
                'icir': _round(icir, 4),
                'annualized_icir': _round(ann_icir, 4),
                'win_rate': _round(float((ic_df['ic'] > 0).mean()), 4),
                'latest_ic': _round(float(latest['ic']), 5),
                'latest_rolling_ic': _round(float(latest_roll_ic.iloc[-1]), 5) if len(latest_roll_ic) else None,
                'latest_rolling_icir': _round(float(latest_rolling.iloc[-1]), 4) if len(latest_rolling) else None,
                'n_days': int(len(ic_df)),
                'avg_universe_size': int(round(float(ic_df['n'].mean()))),
            },
            'series': series,
            'coverage': {
                'factor_start': str(fv['date'].min()),
                'factor_end': str(fv['date'].max()),
                'price_start': str(close.index.min()) if len(close.index) else None,
                'price_end': str(close.index.max()) if len(close.index) else None,
                'ic_start': str(ic_df['date'].min()),
                'ic_end': str(ic_df['date'].max()),
            },
            'warnings': warnings,
        }
    finally:
        con.close()


@router.get('/{account_id}')
async def signal_quality(
    account_id: str,
    horizon: int = Query(5, ge=1, le=60),
    window: int = Query(20, ge=5, le=120),
    market: str = Query('US'),
):
    market = _validate_market(market)
    key = (account_id, market, horizon, window)
    now = time.time()
    async with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            out = dict(cached[1])
            out['cached'] = True
            out['ttl_remaining'] = int(_CACHE_TTL - (now - cached[0]))
            return out

    result = await asyncio.to_thread(_compute_signal_quality_sync, account_id, market, horizon, window)
    async with _CACHE_LOCK:
        _CACHE[key] = (time.time(), result)
    out = dict(result)
    out['cached'] = False
    out['ttl_remaining'] = _CACHE_TTL
    return out


@router.get('/{account_id}/decay')
async def signal_quality_decay(
    account_id: str,
    horizons: str = Query('1,5,10,20'),
    window: int = Query(20, ge=5, le=120),
    market: str = Query('US'),
):
    market = _validate_market(market)
    hs: list[int] = []
    for part in horizons.split(','):
        try:
            h = int(part.strip())
        except ValueError:
            continue
        if 1 <= h <= 60 and h not in hs:
            hs.append(h)
    if not hs:
        raise HTTPException(status_code=400, detail='no valid horizons')
    rows = []
    for h in hs[:8]:
        r = await signal_quality(account_id, horizon=h, window=window, market=market)
        rows.append({
            'horizon': h,
            'supported': r.get('supported'),
            'status': r.get('status'),
            'summary': r.get('summary') or {},
            'warnings': r.get('warnings') or [],
        })
    return {'account_id': account_id, 'market': market, 'window': window, 'decay': rows}
