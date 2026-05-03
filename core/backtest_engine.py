"""Real historical backtest engine (Qlib-style).

Replays history in [start_date, end_date]:
  1. Fetch OHLCV for all universe tickers from yfinance.
  2. For each trading day, compute factors, generate signals, execute trades
     with cost model, mark-to-market.
  3. Emit progress to a shared job store so the frontend can poll.

Reuses quant-trading's FactorEngine, SignalGenerator, GPSignalGenerator,
TradingEngine, MoomooAUCosts, and the per-account strategy configs.
"""
from __future__ import annotations

import os
import sys
import math
import uuid
import asyncio
import logging
import traceback
from datetime import datetime
from typing import Callable

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.expanduser("~/quant-trading")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

log = logging.getLogger("dashboard.backtest")


def _looks_like_q(acct_id: str) -> bool:
    """True if the account id is a Qlib model account (Q01-Q10 or CQ01-CQ10).

    Used by run_backtest_job to short-circuit with a clear error rather
    than the generic 'no valid account' message — Q-account replay is
    blocked until daily checkpoint coverage is sufficient.
    """
    if not acct_id:
        return False
    s = acct_id.upper()
    if s.startswith("CQ") and len(s) >= 4 and s[2:].isdigit():
        return True
    if s.startswith("Q") and len(s) >= 3 and s[1:].isdigit():
        return True
    return False


# ---- Job store ---------------------------------------------------------------

JOBS: dict[str, dict] = {}  # job_id -> {status, progress, message, result, error}


def _set(job_id: str, **kw):
    if job_id in JOBS:
        JOBS[job_id].update(kw)


def new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "status": "pending",     # pending | running | done | error
        "progress": 0.0,         # 0..100
        "message": "初始化",
        "result": None,
        "error": None,
    }
    return job_id


def get_job(job_id: str) -> dict | None:
    return JOBS.get(job_id)


# ---- Core backtest -----------------------------------------------------------

def _load_quant_modules():
    """Import quant-trading deps lazily so module import doesn't hard-fail."""
    from dataclasses import replace as _dc_replace
    from config.settings import (
        STOCK_UNIVERSE, UNIVERSES, BENCHMARKS_BY_MARKET,
        ACCOUNT_PREFIX, INITIAL_CASH,
    )
    from data.fetcher import DataFetcher
    from factors.alpha_factors import FactorEngine
    from factors.signal import SignalGenerator
    from factors.gp_signal import GPSignalGenerator
    from factors.gp_miner import GPAlphaMiner
    from trading.engine import TradingEngine
    from trading.costs import MoomooAUCosts, CNCosts
    from accounts.strategies import STRATEGIES
    from accounts.gp_strategies import GP_STRATEGIES
    return dict(
        STOCK_UNIVERSE=STOCK_UNIVERSE, UNIVERSES=UNIVERSES,
        BENCHMARKS_BY_MARKET=BENCHMARKS_BY_MARKET,
        ACCOUNT_PREFIX=ACCOUNT_PREFIX, INITIAL_CASH=INITIAL_CASH,
        DataFetcher=DataFetcher,
        FactorEngine=FactorEngine, SignalGenerator=SignalGenerator,
        GPSignalGenerator=GPSignalGenerator, GPAlphaMiner=GPAlphaMiner,
        TradingEngine=TradingEngine,
        MoomooAUCosts=MoomooAUCosts, CNCosts=CNCosts,
        STRATEGIES=STRATEGIES, GP_STRATEGIES=GP_STRATEGIES,
        dc_replace=_dc_replace,
    )


def _execute_alpha_trades(engine, strat, acct, signals, current_prices):
    buy_tickers = {t for t, _ in signals["buy"][:strat.top_n]}
    for ticker, shares in list(acct.get_positions().items()):
        if shares <= 0:
            continue
        price = current_prices.get(ticker)
        if not price:
            continue
        pos = acct._positions.get(ticker)
        if pos and pos.avg_cost > 0:
            if (price - pos.avg_cost) / pos.avg_cost <= -strat.stop_loss:
                try: engine.execute_signal(strat.id, ticker, "sell", shares, price, current_prices)
                except Exception: pass
                continue
        if ticker not in buy_tickers:
            try: engine.execute_signal(strat.id, ticker, "sell", shares, price, current_prices)
            except Exception: pass
    equity = acct.get_equity(current_prices)
    target = equity * strat.max_position_pct
    for ticker, _ in signals["buy"][:strat.top_n]:
        price = current_prices.get(ticker)
        if not price or price <= 0: continue
        held = acct.get_positions().get(ticker, 0)
        held_val = held * price
        if held_val >= target * 0.9: continue
        budget = min(target - held_val, acct.cash * 0.95)
        if budget < 5: continue
        shares = int(budget / price)
        if shares <= 0: continue
        try: engine.execute_signal(strat.id, ticker, "buy", shares, price, current_prices)
        except Exception: pass


def _execute_gp_trades(engine, gp_strat, acct, signals, current_prices):
    buy_tickers = set(signals.get("buy", []))
    sell_tickers = set(signals.get("sell", []))
    for ticker, shares in list(acct.get_positions().items()):
        if shares <= 0: continue
        price = current_prices.get(ticker)
        if not price: continue
        pos = acct._positions.get(ticker)
        if pos and pos.avg_cost > 0:
            if (price - pos.avg_cost) / pos.avg_cost <= -gp_strat.stop_loss:
                try: engine.execute_signal(gp_strat.id, ticker, "sell", shares, price, current_prices)
                except Exception: pass
                continue
        if ticker not in buy_tickers or ticker in sell_tickers:
            try: engine.execute_signal(gp_strat.id, ticker, "sell", shares, price, current_prices)
            except Exception: pass
    equity = acct.get_equity(current_prices)
    target = equity * gp_strat.max_position_pct
    for ticker in list(signals.get("buy", []))[:gp_strat.top_n]:
        price = current_prices.get(ticker)
        if not price or price <= 0: continue
        held = acct.get_positions().get(ticker, 0)
        held_val = held * price
        if held_val >= target * 0.9: continue
        budget = min(target - held_val, acct.cash * 0.95)
        if budget < 5: continue
        shares = int(budget / price)
        if shares <= 0: continue
        try: engine.execute_signal(gp_strat.id, ticker, "buy", shares, price, current_prices)
        except Exception: pass


def _filter_gp_factors(gp_strat, gp_factors_dict, mined_factors):
    if not mined_factors or not gp_factors_dict:
        return gp_factors_dict
    sorted_by_ic = sorted(mined_factors, key=lambda f: abs(f["ic"]), reverse=True)
    all_names = [f["name"] for f in sorted_by_ic]
    if gp_strat.factor_selection == "top5":
        keep = set(all_names[:5])
    elif gp_strat.factor_selection == "top10":
        keep = set(all_names[:10])
    elif gp_strat.factor_selection == "bottom5":
        keep = set(all_names[-5:]) if len(all_names) >= 5 else set(all_names)
    else:
        keep = set(all_names)
    if gp_strat.scoring_method == "top3_only":
        keep = keep & set(all_names[:3]) if len(all_names) >= 3 else keep
    filtered = {}
    for ticker, fdf in gp_factors_dict.items():
        if fdf is None or fdf.empty:
            filtered[ticker] = fdf; continue
        cols = [c for c in fdf.columns if c in keep]
        filtered[ticker] = fdf[cols] if cols else fdf
    return filtered


def _stats_from_curve(equity_curve, initial_capital, total_trades, wins, losses, gross_profit, gross_loss, total_pnl):
    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_capital
    total_return = (final_equity / initial_capital - 1) * 100 if initial_capital else 0
    if len(equity_curve) >= 2:
        first_ts = equity_curve[0]["timestamp"][:10]
        last_ts = equity_curve[-1]["timestamp"][:10]
        try:
            days = (datetime.fromisoformat(last_ts) - datetime.fromisoformat(first_ts)).days
        except Exception:
            days = 0
        annualized = ((final_equity / initial_capital) ** (365 / days) - 1) * 100 if days > 0 else 0
    else:
        annualized = 0
    peak = 0; max_dd = 0
    for pt in equity_curve:
        if pt["equity"] > peak: peak = pt["equity"]
        dd = (peak - pt["equity"]) / peak if peak else 0
        if dd > max_dd: max_dd = dd
    daily_returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i-1]["equity"]
        if prev != 0:
            daily_returns.append(equity_curve[i]["equity"] / prev - 1)
    if len(daily_returns) >= 2:
        mean_r = sum(daily_returns) / len(daily_returns)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1))
        sharpe = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0
        downside = [r for r in daily_returns if r < 0]
        if downside:
            down_std = math.sqrt(sum(r ** 2 for r in downside) / len(downside))
            sortino = (mean_r / down_std) * math.sqrt(252) if down_std > 0 else 0
        else: sortino = 0
    else:
        sharpe = 0; sortino = 0
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999999 if gross_profit > 0 else 0)
    return {
        "total_return": round(total_return, 4),
        "annualized_return": round(annualized, 4),
        "max_drawdown": round(max_dd * 100, 4),
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4),
        "total_trades": total_trades,
    }


def _account_trade_stats(acct):
    """Extract win/loss from account's trade_log (realized PnL on sells)."""
    wins = losses = 0
    gp = gl = 0.0; total_pnl = 0.0
    avg_cost: dict[str, float] = {}
    pos: dict[str, float] = {}
    for t in acct.trade_log:
        side = (t.get("side") or "").upper()
        shares = t.get("shares", 0)
        price = t.get("price", 0)
        ticker = t.get("ticker") or t.get("symbol")
        cost = t.get("cost", 0) or 0
        if side == "BUY":
            old_s = pos.get(ticker, 0); old_c = avg_cost.get(ticker, 0)
            new_s = old_s + shares
            if new_s > 0:
                avg_cost[ticker] = (old_c * old_s + price * shares) / new_s
            pos[ticker] = new_s
        elif side == "SELL":
            avg_c = avg_cost.get(ticker, price)
            pnl = (price - avg_c) * shares - cost
            total_pnl += pnl
            if pnl >= 0: wins += 1; gp += pnl
            else: losses += 1; gl += abs(pnl)
            pos[ticker] = pos.get(ticker, 0) - shares
    return wins, losses, gp, gl, total_pnl


def _run_single_account(acct_id, strat_obj, is_gp, all_data, sim_dates, costs, initial_capital,
                        mods, mined_factors_map, progress_cb: Callable[[float, str], None],
                        interval: str = "1d",
                        prefactors: dict | None = None):
    """Backtest one account.

    prefactors: optional {ticker: factor_df} precomputed once over the full window.
    When provided, we slice by date instead of recomputing per bar (O(bars) vs O(bars²)).
    """
    FactorEngine = mods["FactorEngine"]; TradingEngine = mods["TradingEngine"]
    SignalGenerator = mods["SignalGenerator"]; GPSignalGenerator = mods["GPSignalGenerator"]
    GPAlphaMiner = mods["GPAlphaMiner"]

    # Mutable holder so the trade_callback can stamp trades with the current bar timestamp.
    _ctx = {"ts": None}

    def _stamp_trade(account_name, trade):
        # Called immediately after each successful execute_signal.
        trade["timestamp"] = _ctx["ts"]

    engine = TradingEngine(
        max_position_pct=strat_obj.max_position_pct,
        stop_loss_pct=-strat_obj.stop_loss,
        costs=costs,
        trade_callback=_stamp_trade,
    )
    acct = engine.create_account(acct_id, initial_cash=initial_capital)
    factor_engine = FactorEngine()
    sig_gen = SignalGenerator(buy_top=10, sell_top=10)
    gp_sig_gen = GPSignalGenerator()
    gp_miner = GPAlphaMiner()
    mined_factors = mined_factors_map.get(acct_id, []) if is_gp else []

    equity_curve = []
    snapshots = []  # per-bar holdings snapshot for hover tooltip
    rebalance_counter = 0
    n = len(sim_dates)
    hours_per_bar = 7 if interval == "1d" else 1
    def _fmt_ts(d):
        s = str(d)
        return s[:10] if interval == "1d" else s[:16]

    for i, date in enumerate(sim_dates):
        _ctx["ts"] = _fmt_ts(date)
        # Fast path: slice precomputed factors by date (alpha strategies only).
        current_prices = {}
        factors_to_date = {}
        data_to_date = {}
        for ticker, df in all_data.items():
            # current price = latest bar at-or-before `date`
            idx = df.index.searchsorted(date, side="right")
            if idx < 20: continue
            current_prices[ticker] = float(df["close"].iloc[idx - 1])
            if prefactors is not None and ticker in prefactors:
                factors_to_date[ticker] = prefactors[ticker].iloc[:idx]
            else:
                data_to_date[ticker] = df.iloc[:idx]

        if not current_prices:
            equity_curve.append({"timestamp": _fmt_ts(date), "equity": initial_capital})
            continue

        rebalance_counter += hours_per_bar
        should_rebalance = rebalance_counter >= strat_obj.rebalance_hours
        if should_rebalance:
            rebalance_counter = 0
            try:
                if is_gp and mined_factors:
                    gp_factors = gp_miner.compute_gp_factors(data_to_date, mined_factors)
                    filtered = _filter_gp_factors(strat_obj, gp_factors, mined_factors)
                    signals = gp_sig_gen.generate_signals(filtered, top_n=strat_obj.top_n)
                    _execute_gp_trades(engine, strat_obj, acct, signals, current_prices)
                elif not is_gp:
                    # Prefer precomputed factors (vectorized path)
                    if factors_to_date:
                        signals = sig_gen.generate_signals(factors_to_date, strat_obj.strategy_type)
                    else:
                        fdict = factor_engine.compute_multi(data_to_date)
                        signals = sig_gen.generate_signals(fdict, strat_obj.strategy_type)
                    _execute_alpha_trades(engine, strat_obj, acct, signals, current_prices)
            except Exception as e:
                log.warning("rebalance failed %s on %s: %s", acct_id, date, e)

        equity = acct.get_equity(current_prices)
        ts_str = _fmt_ts(date)
        equity_curve.append({"timestamp": ts_str, "equity": round(equity, 2)})

        # Per-bar holdings snapshot (used for hover tooltip on the frontend)
        holdings = []
        for t, p in acct._positions.items():
            px = current_prices.get(t, p.avg_cost)
            mv = p.shares * px
            pnl_pct = ((px / p.avg_cost) - 1) * 100 if p.avg_cost > 0 else 0.0
            holdings.append({
                "ticker": t,
                "shares": round(p.shares, 4),
                "avg_cost": round(p.avg_cost, 4),
                "price": round(px, 4),
                "value": round(mv, 2),
                "pnl_pct": round(pnl_pct, 2),
            })
        holdings.sort(key=lambda h: h["value"], reverse=True)
        snapshots.append({
            "timestamp": ts_str,
            "cash": round(acct.cash, 2),
            "equity": round(equity, 2),
            "holdings": holdings,
        })

        if i % 20 == 0 or i == n - 1:
            progress_cb((i + 1) / n, f"{acct_id} @ {ts_str}")

    wins, losses, gp_, gl_, total_pnl = _account_trade_stats(acct)
    stats = _stats_from_curve(equity_curve, initial_capital, len(acct.trade_log),
                               wins, losses, gp_, gl_, total_pnl)
    # Serialize trade_log (strip non-JSON-safe values, keep core fields)
    trades_out = []
    for t in acct.trade_log:
        trades_out.append({
            "timestamp": t.get("timestamp"),
            "side": t.get("side"),
            "ticker": t.get("ticker") or t.get("symbol"),
            "shares": round(float(t.get("shares") or 0), 4),
            "price": round(float(t.get("price") or 0), 4),
            "amount": round(float(t.get("amount") or 0), 2),
            "fees": round(float(t.get("total_fees") or t.get("fees") or 0), 4),
        })
    return {
        "account_id": acct_id,
        "strategy_name": strat_obj.name,
        "equity_curve": equity_curve,
        "stats": stats,
        "initial_capital": initial_capital,
        "trades": trades_out,
        "snapshots": snapshots,
    }


def _build_combined(per_account, initial_capital):
    if not per_account:
        return {"equity_curve": [], "stats": _stats_from_curve([], initial_capital, 0, 0, 0, 0, 0, 0)}
    # collect all timestamps
    ts_set = set()
    for a in per_account:
        for pt in a["equity_curve"]:
            ts_set.add(pt["timestamp"])
    all_ts = sorted(ts_set)
    # forward-fill per account
    filled = {}
    for a in per_account:
        curve = sorted(a["equity_curve"], key=lambda p: p["timestamp"])
        idx = 0; last = initial_capital
        m = {}
        for ts in all_ts:
            while idx < len(curve) and curve[idx]["timestamp"] <= ts:
                last = curve[idx]["equity"]; idx += 1
            m[ts] = last
        filled[a["account_id"]] = m
    combined_curve = [{"timestamp": ts, "equity": sum(m[ts] for m in filled.values())} for ts in all_ts]
    n = len(per_account)
    combined_initial = initial_capital * n
    stats = _stats_from_curve(combined_curve, combined_initial,
                               sum(a["stats"]["total_trades"] for a in per_account),
                               0, 0, 0, 0, 0)
    return {"equity_curve": combined_curve, "stats": stats}


async def run_backtest_job(job_id: str, account_ids: list[str], start_date: str,
                            end_date: str, initial_capital: float,
                            universe_size: int = 100, market: str = 'US'):
    try:
        market = (market or 'US').upper()
        _set(job_id, status="running", progress=1, message=f"加载策略与数据 ({market})...")
        mods = _load_quant_modules()
        STRATEGIES = mods["STRATEGIES"]; GP_STRATEGIES = mods["GP_STRATEGIES"]
        GPAlphaMiner = mods["GPAlphaMiner"]
        FactorEngine = mods["FactorEngine"]
        dc_replace = mods["dc_replace"]
        ACCOUNT_PREFIX = mods["ACCOUNT_PREFIX"]
        BENCHMARKS_BY_MARKET = mods["BENCHMARKS_BY_MARKET"]

        # Per-market strategy IDs (US: A01.., CN: CA01..)
        prefix = ACCOUNT_PREFIX.get(market, "")
        strats_for_market = [dc_replace(s, id=f"{prefix}{s.id}") for s in STRATEGIES]
        gp_strats_for_market = [dc_replace(s, id=f"{prefix}{s.id}") for s in GP_STRATEGIES]

        # Per-market cost model
        if market == 'CN':
            costs = mods["CNCosts"]()
        else:
            costs = mods["MoomooAUCosts"]()

        # Liquid-universe cut: default top 100 by 60d ADV (US).
        # CN uses static UNIVERSES['CN'] (沪深300 already curated).
        from core.universe import load_liquid_universe
        from core.price_cache import get_history, estimate_fetch_cost
        universe = load_liquid_universe(top_n=universe_size, market=market)
        if not universe:
            _set(job_id, status="error", error=f"market={market} 选股池为空", progress=100)
            return
        _set(job_id, progress=2,
             message=(f"[{market}] 选股池: {len(universe)} 支" if market == 'CN'
                      else f"选股池: top {len(universe)} 流动性（按 60d 平均成交额）"))

        strat_map: dict[str, tuple[object, bool]] = {}
        for s in strats_for_market: strat_map[s.id] = (s, False)
        for s in gp_strats_for_market: strat_map[s.id] = (s, True)

        wanted = [a for a in account_ids if a in strat_map]
        # Q-accounts (Qlib ML models) are NOT yet supported in backtest.
        # See trading-dashboard /api/backtest/qlib-status — three look-ahead
        # vectors (model weights, feature normalization, label) need a
        # walk-forward retrain or frozen daily checkpoints. We started
        # capturing checkpoints on 2026-04-30; replay backtest will land
        # once we accumulate ~20 trading days of coverage.
        q_requested = [a for a in account_ids
                       if a not in strat_map and _looks_like_q(a)]
        if q_requested:
            _set(job_id, status="error",
                 error=("Q组账户暂不支持回测（Qlib模型存在前瞻偏差风险，"
                        "已启动每日 checkpoint 累积，详见回测页顶部说明）。"
                        f"被拒账户: {', '.join(q_requested)}"),
                 progress=100)
            return
        if not wanted:
            _set(job_id, status="error",
                 error=f"无有效账户 (market={market}, 期望前缀 '{prefix}')",
                 progress=100)
            return

        start_dt = datetime.fromisoformat(start_date[:10])
        end_dt = datetime.fromisoformat(end_date[:10])

        days_since_start = (datetime.utcnow() - start_dt).days
        if days_since_start <= 725:
            interval = "1h"
            warmup_days = 30
        else:
            interval = "1d"
            warmup_days = 90

        # CN intraday cache only carries 1d (and 15m via separate backfill);
        # 1h CN bars are sparse + slow via akshare. Force daily for CN until
        # we backfill 1h. Same code path otherwise.
        if market == 'CN':
            interval = "1d"
            warmup_days = 90

        warm_start = (start_dt - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")

        # --- Pre-flight estimate ------------------------------------------
        est = estimate_fetch_cost(universe, warm_start, end_str, interval=interval)
        _set(job_id, progress=3,
             message=(f"[预估] 缓存命中 {est['cached_full']}/"
                      f"部分 {est['cached_partial']}/缺失 {est['missing']} 支 | "
                      f"需下载≈{est['est_bars_to_download']:,} bars "
                      f"≈{est['est_mb_to_download']} MB"))

        def _cache_progress(frac, msg):
            _set(job_id, progress=round(3 + 27 * frac, 1), message=msg)

        data_stats: dict = {}
        def _sync_fetch():
            return get_history(universe, warm_start, end_str, interval=interval,
                               progress_cb=_cache_progress,
                               min_rows=50 if interval == "1h" else 30,
                               stats_out=data_stats, market=market)

        all_data = await asyncio.to_thread(_sync_fetch)
        if not all_data:
            _set(job_id, status="error", error="无法获取任何历史数据", progress=100)
            return

        # --- Benchmarks: per-market, same interval/window ------------------
        bench_defs = BENCHMARKS_BY_MARKET.get(market, [])
        bench_tickers = [b["ticker"] for b in bench_defs]
        bench_stats: dict = {}
        bench_data: dict = {}
        if bench_tickers:
            def _sync_bench():
                return get_history(bench_tickers, warm_start, end_str, interval=interval,
                                   min_rows=10, stats_out=bench_stats, market=market)
            bench_data = await asyncio.to_thread(_sync_bench)

        # --- Vectorized factor precomputation (alpha accounts only) -------
        needs_alpha_factors = any(not strat_map[a][1] for a in wanted)
        prefactors: dict = {}
        if needs_alpha_factors:
            _set(job_id, progress=31, message=f"预计算因子 (Alpha158, {len(all_data)} 支)...")
            def _sync_factors():
                fe = FactorEngine()
                out = {}
                for t, df in all_data.items():
                    try: out[t] = fe.compute_all(df)
                    except Exception: pass
                return out
            prefactors = await asyncio.to_thread(_sync_factors)
            _set(job_id, progress=38, message=f"因子就绪: {len(prefactors)} 支")

        # sim bars
        all_dates = set()
        for df in all_data.values():
            all_dates.update(df.index.tolist())
        all_dates = sorted(all_dates)
        sim_dates = [d for d in all_dates
                     if pd.Timestamp(start_dt) <= d <= pd.Timestamp(end_dt) + pd.Timedelta(days=1)]
        if not sim_dates:
            _set(job_id, status="error",
                 error=f"在 {start_date}~{end_date} 内无 {interval} 数据", progress=100)
            return

        _set(job_id, progress=40,
             message=f"模拟 {len(sim_dates)} 根 {interval} K × {len(wanted)} 账户 (universe={len(all_data)})")

        # costs already constructed above (per-market: CNCosts vs MoomooAUCosts)
        mined_factors_map = GPAlphaMiner.load_per_account_factors() or {}

        per_account = []
        n_accounts = len(wanted)
        base_pct = 40
        span_pct = 57

        for idx, acct_id in enumerate(wanted):
            strat_obj, is_gp = strat_map[acct_id]
            acct_base = base_pct + span_pct * idx / n_accounts
            acct_span = span_pct / n_accounts
            def make_cb(b, s, aid):
                def cb(frac, msg):
                    pct = b + s * frac
                    _set(job_id, progress=round(pct, 1),
                         message=f"[{idx+1}/{n_accounts}] {aid}: {msg}")
                return cb
            cb = make_cb(acct_base, acct_span, acct_id)

            try:
                res = await asyncio.to_thread(
                    _run_single_account,
                    acct_id, strat_obj, is_gp, all_data, sim_dates, costs,
                    initial_capital, mods, mined_factors_map, cb, interval,
                    prefactors if not is_gp else None,
                )
                per_account.append(res)
            except Exception as e:
                log.error("backtest account %s failed: %s\n%s", acct_id, e, traceback.format_exc())
                per_account.append({
                    "account_id": acct_id,
                    "strategy_name": strat_obj.name,
                    "equity_curve": [{"timestamp": start_date[:10], "equity": initial_capital}],
                    "stats": _stats_from_curve([{"timestamp": start_date[:10], "equity": initial_capital}],
                                               initial_capital, 0, 0, 0, 0, 0, 0),
                    "initial_capital": initial_capital,
                    "trades": [],
                    "snapshots": [],
                    "error": str(e),
                })

        _set(job_id, progress=98, message="汇总组合权益曲线 ...")
        combined = _build_combined(per_account, initial_capital)

        # Build benchmark equity curves (normalized to initial_capital)
        benchmarks = []
        sim_ts_set = {pt["timestamp"] for a in per_account for pt in a.get("equity_curve", [])}
        for bdef in bench_defs:
            sym = bdef["ticker"]
            bdf = bench_data.get(sym)
            if bdf is None or bdf.empty:
                continue
            # slice to sim window and normalize
            bslice = bdf.loc[(bdf.index >= pd.Timestamp(start_dt)) &
                             (bdf.index <= pd.Timestamp(end_dt) + pd.Timedelta(days=1))]
            if bslice.empty:
                continue
            base_px = float(bslice["close"].iloc[0])
            if base_px <= 0:
                continue
            def _fmt(d):
                s = str(d); return s[:10] if interval == "1d" else s[:16]
            curve = [{"timestamp": _fmt(idx),
                      "equity": round(initial_capital * float(row) / base_px, 2)}
                     for idx, row in bslice["close"].items()]
            benchmarks.append({"symbol": sym, "label": bdef.get("name", sym),
                               "equity_curve": curve})

        result = {"accounts": per_account, "combined": combined,
                  "benchmarks": benchmarks,
                  "meta": {"start": start_date[:10], "end": end_date[:10],
                           "market": market,
                           "sim_bars": len(sim_dates), "n_accounts": len(per_account),
                           "universe_size": len(all_data),
                           "interval": interval,
                           "estimate": est,
                           "data_stats": data_stats,
                           "benchmark_stats": bench_stats,
                           "warning": (None if interval == "1h"
                                       else "起始日期超过 yfinance 1h 限制 (~730天)，已降级为日K；与实盘1h粒度有偏差")}}
        _set(job_id, status="done", progress=100, message="完成", result=result)
    except Exception as e:
        log.error("backtest job failed: %s\n%s", e, traceback.format_exc())
        _set(job_id, status="error", error=str(e), progress=100)
