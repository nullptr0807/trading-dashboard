"""Benchmark price fetcher for QQQ / SPY with on-disk caching.

We need benchmark prices aligned to the strategy's trading window,
but IDX1/IDX2 snapshots in the local DB only cover a short span.
This module fetches hourly QQQ/SPY from yfinance and caches per-day
parquet/json files under ~/trading-dashboard/data/benchmarks/.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_CACHE_DIR = Path(__file__).resolve().parent.parent / 'data' / 'benchmarks'
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_TTL_SECONDS = 15 * 60  # refresh every 15 min

# Per-market benchmark mapping — used by API consumers when adding overlay
# curves to equity charts. US shows QQQ+SPY (IDX1/IDX2 in our account naming);
# CN shows CSI 300 (IDX3, ticker 000300.SH).
MARKET_BENCHMARKS: dict[str, list[dict]] = {
    'US': [
        {'ticker': 'QQQ',        'label': 'QQQ',       'account_id': 'IDX1'},
        {'ticker': 'SPY',        'label': 'SPY',       'account_id': 'IDX2'},
    ],
    'CN': [
        {'ticker': '000300.SH',  'label': '沪深300',    'account_id': 'IDX3'},
    ],
}


def benchmarks_for(market: str) -> list[dict]:
    return MARKET_BENCHMARKS.get((market or 'US').upper(), MARKET_BENCHMARKS['US'])

_mem_cache: dict[str, dict] = {}   # ticker -> {'fetched_at': ts, 'bars': [{ts, close}]}
_lock = asyncio.Lock()


def _cache_file(ticker: str, interval: str) -> Path:
    return _CACHE_DIR / f'{ticker}_{interval}.json'


def _now() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


def _load_disk(ticker: str, interval: str) -> Optional[dict]:
    f = _cache_file(ticker, interval)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
        return data
    except Exception:
        return None


def _save_disk(ticker: str, interval: str, data: dict):
    try:
        _cache_file(ticker, interval).write_text(json.dumps(data))
    except Exception:
        pass


def _fetch_sync(ticker: str, start: datetime, interval: str = '5m') -> list[dict]:
    """Blocking fetch via the shared quant SQLite cache. Returns list of
    {timestamp, close} bars at the given interval."""
    from core.price_cache import get_history
    s = (start - timedelta(days=1))
    start_str = s.strftime('%Y-%m-%d')
    end_str = (datetime.now(tz=timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%d')
    try:
        data = get_history([ticker], start_str, end_str, interval=interval, min_rows=1)
        df = data.get(ticker)
        if df is None or df.empty:
            return []
        bars = []
        for idx, val in df['close'].items():
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            if fval != fval:  # NaN
                continue
            ts = idx.tz_localize('UTC').isoformat() if idx.tzinfo is None else idx.isoformat()
            bars.append({'timestamp': ts, 'close': fval})
        return bars
    except Exception as e:
        print(f'[benchmarks] fetch {ticker} {interval} failed: {e}')
        return []


_INTRADAY_TTL = 5 * 60       # 5m bars: refresh every 5 min
_DAILY_TTL = 6 * 3600        # 1d bars: refresh every 6 h


async def _get_interval_bars(ticker: str, interval: str, since: datetime) -> list[dict]:
    """Get cached bars for one (ticker, interval) since `since` datetime."""
    ttl = _DAILY_TTL if interval == '1d' else _INTRADAY_TTL
    key = f'{ticker}|{interval}'
    async with _lock:
        entry = _mem_cache.get(key)
        fresh = entry and (_now() - entry['fetched_at'] < ttl)
        if not fresh:
            disk = _load_disk(ticker, interval)
            if disk and _now() - disk.get('fetched_at', 0) < ttl:
                entry = disk
                _mem_cache[key] = entry
                fresh = True
        if not fresh:
            bars = await asyncio.to_thread(_fetch_sync, ticker, since, interval)
            entry = {'fetched_at': _now(), 'bars': bars}
            _mem_cache[key] = entry
            _save_disk(ticker, interval, entry)
    return list(entry.get('bars', []))


async def get_bars(ticker: str, since_iso: str) -> list[dict]:
    """Return list of {timestamp, close} for ticker since since_iso.

    Strategy: yfinance 5m only goes back ~60 days. So we splice:
      - [since, today-55d): use 1d daily bars
      - [today-55d, now]:   use 5m intraday bars (with prepost)
    If `since` is within 60 days, we use 5m for the whole range.
    """
    try:
        cutoff = datetime.fromisoformat(since_iso.replace('Z', '+00:00'))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    except Exception:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=90)

    now = datetime.now(tz=timezone.utc)
    splice_point = now - timedelta(days=55)  # 5m yfinance limit is 60d, leave buffer
    # CN tickers (.SH/.SZ) aren't quoted by yfinance at 5m granularity, and our
    # SQLite cache only maintains 15m for them (backfill_cn_15min.py + cron).
    # Use 15m as the intraday interval for CN; US still uses 5m.
    cn_suffixes = ('.SH', '.SZ', '.BJ')
    intraday_interval = '15m' if ticker.upper().endswith(cn_suffixes) else '5m'

    out: list[dict] = []
    if cutoff < splice_point:
        # Need long-range 1d bars first
        daily = await _get_interval_bars(ticker, '1d', cutoff)
        for b in daily:
            try:
                t = datetime.fromisoformat(b['timestamp'].replace('Z', '+00:00'))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if cutoff <= t < splice_point:
                    out.append(b)
            except Exception:
                continue

    intraday_since = max(cutoff, splice_point)
    intra = await _get_interval_bars(ticker, intraday_interval, intraday_since)
    for b in intra:
        try:
            t = datetime.fromisoformat(b['timestamp'].replace('Z', '+00:00'))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t >= intraday_since:
                out.append(b)
        except Exception:
            continue

    # Sort + dedup
    seen = {}
    for b in out:
        seen[b['timestamp']] = b['close']
    merged = [{'timestamp': k, 'close': v} for k, v in seen.items()]
    merged.sort(key=lambda x: x['timestamp'])
    return merged


async def rebased_curve(ticker: str, since_iso: str, initial: float = 10000.0,
                        align_to: list[str] | None = None) -> list[dict]:
    """Return [{equity, timestamp}] rebased so first bar == initial.

    If align_to is given (list of ISO timestamps), the curve is forward-filled
    onto those timestamps so benchmark values exist outside regular trading
    hours (shown as flat segments on the chart).
    """
    bars = await get_bars(ticker, since_iso)
    if not bars:
        return []
    base = bars[0]['close']
    if not base:
        return []

    rebased = [
        {'equity': round(initial * b['close'] / base, 2),
         'timestamp': b['timestamp'],
         '_t': _parse_ts(b['timestamp'])}
        for b in bars
    ]
    rebased = [r for r in rebased if r['_t'] is not None]

    if not align_to:
        return [{'equity': r['equity'], 'timestamp': r['timestamp']} for r in rebased]

    # Forward-fill onto align_to timestamps (outside-market = flat line)
    anchor_t = _parse_ts(since_iso)
    target_ts = []
    for ts in align_to:
        t = _parse_ts(ts)
        if t is None:
            continue
        if anchor_t and t < anchor_t:
            continue
        target_ts.append((t, ts))
    target_ts.sort()

    result = []
    bar_idx = 0
    last_eq = initial  # before first bar: flat at initial
    for t, iso in target_ts:
        while bar_idx < len(rebased) and rebased[bar_idx]['_t'] <= t:
            last_eq = rebased[bar_idx]['equity']
            bar_idx += 1
        result.append({'equity': last_eq, 'timestamp': iso})
    # Also keep the actual bar timestamps so chart shows the real movement points
    for r in rebased:
        result.append({'equity': r['equity'], 'timestamp': r['timestamp']})
    # Dedup by timestamp (keep last write) and sort
    seen = {}
    for r in result:
        seen[r['timestamp']] = r['equity']
    out = [{'equity': v, 'timestamp': k} for k, v in seen.items()]
    out.sort(key=lambda x: x['timestamp'])
    return out


def _parse_ts(s: str):
    try:
        t = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t
    except Exception:
        return None
