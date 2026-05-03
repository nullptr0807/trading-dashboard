"""Price history adapter — delegates to quant-trading's SQLite price cache.

Single source of truth: ~/quant-trading/data/trading.db  (prices table).
This module is a thin adapter that keeps the dashboard's old public API
(get_history, estimate_fetch_cost) but routes all reads/writes through
quant-trading's DataStore + DataFetcher, so:

  - Same DB file → same rows, same normalization, same adjustment basis
  - Two projects automatically share cache hits and incremental downloads
  - No more parquet fork in ~/trading-dashboard/data/prices/

Public API preserved:
  get_history(tickers, start, end, interval='1d', progress_cb=None, ...)
      -> dict[ticker -> DataFrame(indexed by datetime, cols=ohlcv)]
  estimate_fetch_cost(tickers, start, end, interval='1d')
      -> dict of cache stats
"""
from __future__ import annotations

import os
import sys
import logging
from typing import Callable

import pandas as pd

# Bridge into quant-trading so we reuse its DataStore / DataFetcher.
_QUANT_ROOT = os.path.expanduser("~/quant-trading")
if _QUANT_ROOT not in sys.path:
    sys.path.insert(0, _QUANT_ROOT)

from data.store import DataStore            # noqa: E402
from data.fetcher import DataFetcher        # noqa: E402

log = logging.getLogger("dashboard.price_cache")


# ── Fetch-attempt log (prevents re-download of windows where yfinance has no data) ──

def _ensure_fetch_log_table(store: DataStore) -> None:
    """Create price_fetch_log table on first use (idempotent)."""
    conn = store._conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS price_fetch_log (
        ticker TEXT NOT NULL,
        interval TEXT NOT NULL,
        earliest_attempted TEXT NOT NULL,
        latest_attempted   TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (ticker, interval)
    )""")
    conn.commit()
    conn.close()


def _load_fetch_log(store: DataStore, tickers: list[str], interval: str) -> dict:
    """Return {ticker: (earliest_attempted_ts, latest_attempted_ts, fetched_at_ts)}."""
    if not tickers:
        return {}
    _ensure_fetch_log_table(store)
    conn = store._conn()
    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        f"SELECT ticker, earliest_attempted, latest_attempted, fetched_at "
        f"FROM price_fetch_log WHERE interval=? AND ticker IN ({placeholders})",
        [interval, *tickers],
    ).fetchall()
    conn.close()
    out = {}
    for t, e, l, f in rows:
        try:
            out[t] = (pd.Timestamp(e), pd.Timestamp(l), pd.Timestamp(f))
        except Exception:
            pass
    return out


def _record_fetch_attempt(store: DataStore, tickers: list[str],
                          start: pd.Timestamp, end: pd.Timestamp,
                          interval: str) -> None:
    """Merge-record a fetch attempt: widens earliest_attempted / latest_attempted."""
    if not tickers:
        return
    _ensure_fetch_log_table(store)
    now_iso = pd.Timestamp.utcnow().tz_localize(None).isoformat()
    s = start.isoformat()
    e = end.isoformat()
    conn = store._conn()
    # Widen existing window; insert new if absent.
    for t in tickers:
        conn.execute(
            """INSERT INTO price_fetch_log (ticker, interval, earliest_attempted, latest_attempted, fetched_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ticker, interval) DO UPDATE SET
                 earliest_attempted = MIN(earliest_attempted, excluded.earliest_attempted),
                 latest_attempted   = MAX(latest_attempted,   excluded.latest_attempted),
                 fetched_at         = excluded.fetched_at
            """,
            (t, interval, s, e, now_iso),
        )
    conn.commit()
    conn.close()

# yfinance lookback hard limits (days). Kept for backwards-compat imports.
INTERVAL_LIMITS: dict[str, int | None] = {
    "1m": 7, "5m": 60, "15m": 60, "1h": 730, "1d": None,
}
INTERVAL_CHUNK_DAYS: dict[str, int] = {
    "1m": 7, "5m": 60, "15m": 60, "1h": 180, "1d": 3650,
}
INTERVAL_DEFAULT_LOOKBACK: dict[str, int] = {
    "1m": 7, "5m": 60, "15m": 60, "1h": 725, "1d": 365 * 10,
}

# Lazy singletons — one DataStore + per-market DataFetcher for the whole process.
_store: DataStore | None = None
_fetchers: dict[str, object] = {}


def _get_store() -> DataStore:
    global _store
    if _store is None:
        _store = DataStore()
    return _store


def _get_fetcher(market: str = 'US'):
    """Per-market fetcher. US → DataFetcher (yfinance); CN → CNDataFetcher (akshare)."""
    market = (market or 'US').upper()
    f = _fetchers.get(market)
    if f is not None:
        return f
    if market == 'CN':
        from data.cn_fetcher import CNDataFetcher
        f = CNDataFetcher()
    else:
        f = DataFetcher()
    _fetchers[market] = f
    return f


def _normalize_window(start: str, end: str, interval: str):
    need_start = pd.Timestamp(start)
    need_end = pd.Timestamp(end)
    if interval == "1d":
        need_start = need_start.normalize()
        need_end = need_end.normalize()
    else:
        # Intraday: a date-only `end` (midnight) means "through that day", so
        # extend to end-of-day. Otherwise the comparison against the last
        # intraday bar (which is never at midnight) always looks short.
        if need_end == need_end.normalize():
            need_end = need_end + pd.Timedelta(hours=23, minutes=59, seconds=59)
    return need_start, need_end


def _cached_fully_covers(cmin: pd.Timestamp, cmax: pd.Timestamp,
                         need_start: pd.Timestamp, need_end: pd.Timestamp,
                         interval: str,
                         attempt: tuple | None = None) -> bool:
    """Return True if cache already holds every bar yfinance could give us
    for [need_start, need_end].

    Key insights:
      * We can never have bars newer than `now` (the clock), and weekend /
        holiday bars don't exist at all. So the real tail target is
            target_cmax = min(need_end, now) - freshness_slack
        where freshness_slack absorbs weekends so a same-day re-run doesn't
        re-download data yfinance provably won't have.
      * For the head side, if we already tried to fetch back to or past
        `need_start` and that's all yfinance returned (recorded in
        price_fetch_log), trust that — the ticker simply didn't trade that
        early. `attempt` is that log row (earliest_attempted, latest_attempted,
        fetched_at).
    """
    # Head: prefer the wider of (actual cmin, earliest we ever asked yf for).
    head_ok = cmin <= need_start
    if not head_ok and attempt is not None:
        earliest_attempted = attempt[0]
        if earliest_attempted <= need_start:
            head_ok = True
    if not head_ok:
        return False

    # Tail
    now = pd.Timestamp.utcnow().tz_localize(None)
    effective_end = min(need_end, now)
    if interval == "1d":
        freshness = pd.Timedelta(days=4)
    elif interval in ("1h", "60m"):
        # 1h bars must update within ~2h, otherwise intraday charts (and
        # benchmark equity curves on the dashboard) get frozen at the last
        # cached bar — appearing as a flat line for hours after market open.
        freshness = pd.Timedelta(hours=2)
    elif interval in ("15m", "30m"):
        freshness = pd.Timedelta(minutes=45)
    elif interval in ("1m", "5m"):
        freshness = pd.Timedelta(minutes=15)
    else:
        freshness = pd.Timedelta(days=2)
    return cmax >= (effective_end - freshness)


def _to_indexed(df_long: pd.DataFrame, interval: str) -> dict[str, pd.DataFrame]:
    """Convert a long-format DataFrame (ticker/datetime/ohlcv rows) into
    {ticker: DataFrame indexed by datetime with ohlcv columns}.
    Timezone-stripped so downstream code stays naive-UTC like before.
    """
    if df_long is None or df_long.empty:
        return {}
    out: dict[str, pd.DataFrame] = {}
    df_long = df_long.copy()
    df_long["datetime"] = pd.to_datetime(df_long["datetime"], utc=True, errors="coerce")
    df_long = df_long.dropna(subset=["datetime"])
    df_long["datetime"] = df_long["datetime"].dt.tz_convert("UTC").dt.tz_localize(None)
    if interval == "1d":
        df_long["datetime"] = df_long["datetime"].dt.normalize()
    for t, g in df_long.groupby("ticker", sort=False):
        g = g.sort_values("datetime").drop_duplicates("datetime", keep="last")
        sub = g.set_index("datetime")[["open", "high", "low", "close", "volume"]]
        out[str(t)] = sub
    return out


# ── Public API ──────────────────────────────────────────────────────────────

def estimate_fetch_cost(tickers: list[str], start: str, end: str,
                        interval: str = "1d") -> dict:
    """Scan the quant SQLite cache and report what's already present vs. what
    we'd need to download. Reads only coverage metadata — cheap.
    """
    need_start, need_end = _normalize_window(start, end, interval)
    store = _get_store()
    coverage = store.get_price_coverage(tickers, interval=interval)
    attempts = _load_fetch_log(store, tickers, interval)

    cached_full = cached_partial = missing = 0
    need_net: list[str] = []
    missing_bars = 0
    bars_per_day = {"1d": 1, "1h": 7, "15m": 26, "5m": 78, "1m": 390}.get(interval, 7)
    total_window_bars = max(0, (need_end - need_start).days) * bars_per_day

    for t in tickers:
        cov = coverage.get(t)
        if not cov or cov[2] == 0:
            missing += 1
            need_net.append(t)
            missing_bars += total_window_bars
            continue
        try:
            cmin = pd.Timestamp(cov[0])
            cmax = pd.Timestamp(cov[1])
        except Exception:
            missing += 1
            need_net.append(t)
            missing_bars += total_window_bars
            continue
        if getattr(cmin, "tz", None) is not None:
            cmin = cmin.tz_convert("UTC").tz_localize(None)
        if getattr(cmax, "tz", None) is not None:
            cmax = cmax.tz_convert("UTC").tz_localize(None)
        if _cached_fully_covers(cmin, cmax, need_start, need_end, interval,
                                attempt=attempts.get(t)):
            cached_full += 1
        else:
            # Estimate only the actually-missing tail (or head) gap.
            head_gap = max(0, (cmin - need_start).days) if need_start < cmin else 0
            tail_gap = max(0, (need_end - cmax).days)
            gap_days = head_gap + tail_gap
            cached_partial += 1
            need_net.append(t)
            missing_bars += gap_days * bars_per_day

    return {
        "total_tickers": len(tickers),
        "cached_full": cached_full,
        "cached_partial": cached_partial,
        "missing": missing,
        "need_net": len(need_net),
        "est_bars_to_download": int(missing_bars),
        "est_mb_to_download": round(missing_bars * 48 / (1024 * 1024), 2),
    }


def get_history(tickers: list[str], start: str, end: str,
                interval: str = "1d",
                progress_cb: Callable[[float, str], None] | None = None,
                max_workers: int = 8,   # kept for backwards-compat, ignored
                min_rows: int = 30,
                stats_out: dict | None = None,
                market: str = 'US') -> dict[str, pd.DataFrame]:
    """Fetch [start, end] history per ticker/interval via the quant SQLite cache.

    market='US': missing tickers downloaded via yfinance (DataFetcher._fetch_yf_batch).
    market='CN': missing tickers downloaded via akshare (CNDataFetcher._hist_one).
    Both write back to the shared quant SQLite cache.
    """
    market = (market or 'US').upper()
    need_start, need_end = _normalize_window(start, end, interval)
    start_str = need_start.strftime("%Y-%m-%d")
    # DB query uses datetime < end (exclusive), so pad by one day to include end.
    end_exclusive = (need_end + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    store = _get_store()
    fetcher = _get_fetcher(market)

    total = len(tickers)
    coverage = store.get_price_coverage(tickers, interval=interval)
    attempts = _load_fetch_log(store, tickers, interval)

    need_net: list[str] = []
    cached_full_tickers: list[str] = []
    for t in tickers:
        cov = coverage.get(t)
        if not cov or cov[2] == 0:
            need_net.append(t)
            continue
        try:
            cmin = pd.Timestamp(cov[0])
            cmax = pd.Timestamp(cov[1])
        except Exception:
            need_net.append(t)
            continue
        if getattr(cmin, "tz", None) is not None:
            cmin = cmin.tz_convert("UTC").tz_localize(None)
        if getattr(cmax, "tz", None) is not None:
            cmax = cmax.tz_convert("UTC").tz_localize(None)
        if _cached_fully_covers(cmin, cmax, need_start, need_end, interval,
                                attempt=attempts.get(t)):
            cached_full_tickers.append(t)
        else:
            need_net.append(t)

    if progress_cb:
        progress_cb(0.1,
                    f"缓存扫描: 命中 {len(cached_full_tickers)} / 需补 {len(need_net)} ({interval})")

    # Download missing in ONE yfinance batch (quant's _fetch_yf_batch already
    # multiplexes tickers). We download the full [start,end] range — quant's
    # DB uses INSERT OR REPLACE so redundant bars are harmless.
    dl_rows = 0
    dl_tickers = 0
    if need_net:
        if progress_cb:
            progress_cb(0.2, f"下载 {len(need_net)} 支 via {'yfinance' if market=='US' else 'akshare'} ({interval})...")
        if market == 'CN':
            # CN: use the akshare per-ticker path (CNDataFetcher persists internally
            # via save_prices_bulk on its own); call it once with use_cache=False
            # for missing tickers only — it'll fetch + save in one shot.
            from concurrent.futures import ThreadPoolExecutor
            from data.cn_fetcher import _hist_one
            frames = []
            with ThreadPoolExecutor(max_workers=4) as ex_pool:
                for df in ex_pool.map(
                    lambda t: _hist_one(t, start_str, end_exclusive, interval),
                    need_net,
                ):
                    if df is not None and not df.empty:
                        frames.append(df)
            if frames:
                merged = pd.concat(frames, ignore_index=True)
                dl_rows = len(merged)
                dl_tickers = merged["ticker"].nunique()
                store.save_prices_bulk(merged, interval=interval)
        else:
            fetched = fetcher._fetch_yf_batch(need_net, start_str, end_exclusive, interval)
            if not fetched.empty:
                dl_rows = len(fetched)
                dl_tickers = fetched["ticker"].nunique()
                store.save_prices_bulk(fetched, interval=interval)
        # Record the attempt for ALL tickers we tried — even ones the upstream
        # returned nothing for. This prevents re-downloading the same empty
        # head-window next run.
        try:
            _record_fetch_attempt(store, need_net, need_start, need_end, interval)
        except Exception as ex:
            log.warning("price_fetch_log update failed: %s", ex)
        if progress_cb:
            progress_cb(0.7,
                        f"下载完成 {dl_tickers} 支 ({dl_rows} 行) → 写回共享缓存")

    # Re-read everything from DB now that gaps are filled.
    df_long = store.load_prices(tickers, start_str, end_exclusive, interval=interval)
    indexed = _to_indexed(df_long, interval)

    # Slice to [need_start, need_end] (inclusive) and drop tickers with too few bars.
    results: dict[str, pd.DataFrame] = {}
    for t, df in indexed.items():
        sliced = df.loc[(df.index >= need_start) & (df.index <= need_end)]
        if len(sliced) >= min_rows:
            results[t] = sliced

    hit_tickers = len(cached_full_tickers)
    hit_rows = sum(len(results[t]) for t in cached_full_tickers if t in results)

    if progress_cb:
        progress_cb(1.0,
                    f"[{interval}] 缓存命中 {hit_tickers} 支 ({hit_rows} 行) | "
                    f"下载 {dl_tickers} 支 ({dl_rows} 行新数据) | 就绪 {len(results)} 支")
    if stats_out is not None:
        stats_out.update({
            "interval": interval,
            "requested_tickers": total,
            "cache_hit_tickers": hit_tickers,
            "cache_hit_rows": hit_rows,
            "download_tickers": int(dl_tickers),
            "download_rows": int(dl_rows),
            "ready_tickers": len(results),
        })
    return results


# ── Backwards-compat shims ──────────────────────────────────────────────────

def _normalize_index(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Deprecated parquet-era helper — retained only so any lingering import
    doesn't ImportError. New code should not use this.
    """
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    if interval == "1d":
        idx = idx.normalize()
    df.index = idx
    return df.sort_index()
