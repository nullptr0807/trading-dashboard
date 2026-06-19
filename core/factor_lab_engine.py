"""Ad-hoc Alpha158 factor laboratory engine.

This module is deliberately account-free: it reads persisted Alpha158 building
blocks from the shared trading.db, builds a temporary cross-sectional score, and
runs cheap vectorized diagnostics/backtests. It never creates accounts and never
writes to the database.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from core.db import DB_PATH


ALPHA158_FACTORS: list[dict[str, str]] = [
    # KBAR / candlestick shape
    {"name": "KMID", "family": "kbar", "label_zh": "实体涨跌幅", "label_en": "Candle body return", "description_zh": "收盘价相对开盘价的位置。", "description_en": "Close relative to open."},
    {"name": "KLEN", "family": "kbar", "label_zh": "全振幅", "label_en": "Full candle range", "description_zh": "最高价到最低价相对开盘价的振幅。", "description_en": "High-low range relative to open."},
    {"name": "KMID2", "family": "kbar", "label_zh": "实体/振幅", "label_en": "Body over range", "description_zh": "实体长度占当日高低振幅的比例。", "description_en": "Candle body divided by high-low range."},
    {"name": "KUP", "family": "kbar", "label_zh": "上影线", "label_en": "Upper shadow", "description_zh": "上影线相对开盘价的长度。", "description_en": "Upper shadow relative to open."},
    {"name": "KUP2", "family": "kbar", "label_zh": "上影线/振幅", "label_en": "Upper shadow over range", "description_zh": "上影线占当日振幅比例。", "description_en": "Upper shadow divided by high-low range."},
    {"name": "KLOW", "family": "kbar", "label_zh": "下影线", "label_en": "Lower shadow", "description_zh": "下影线相对开盘价的长度。", "description_en": "Lower shadow relative to open."},
    {"name": "KLOW2", "family": "kbar", "label_zh": "下影线/振幅", "label_en": "Lower shadow over range", "description_zh": "下影线占当日振幅比例。", "description_en": "Lower shadow divided by high-low range."},
    {"name": "KSFT", "family": "kbar", "label_zh": "价格重心偏移", "label_en": "Candle shift", "description_zh": "收盘价相对高低区间中点的偏移。", "description_en": "Close relative to the high-low midpoint."},
    {"name": "KSFT2", "family": "kbar", "label_zh": "价格重心偏移/振幅", "label_en": "Shift over range", "description_zh": "价格重心偏移占当日振幅比例。", "description_en": "Candle shift divided by range."},
    # momentum
    {"name": "ROC_5", "family": "momentum", "label_zh": "5日动量", "label_en": "5D return momentum", "description_zh": "过去5个交易日涨跌幅。", "description_en": "Return over the past 5 trading days."},
    {"name": "ROC_10", "family": "momentum", "label_zh": "10日动量", "label_en": "10D return momentum", "description_zh": "过去10个交易日涨跌幅。", "description_en": "Return over the past 10 trading days."},
    {"name": "ROC_20", "family": "momentum", "label_zh": "20日动量", "label_en": "20D return momentum", "description_zh": "过去20个交易日涨跌幅。", "description_en": "Return over the past 20 trading days."},
    {"name": "MA_RATIO_5", "family": "trend", "label_zh": "5日均线比", "label_en": "Price / 5D MA", "description_zh": "收盘价相对5日均线的位置。", "description_en": "Close price relative to its 5-day moving average."},
    {"name": "MA_RATIO_10", "family": "trend", "label_zh": "10日均线比", "label_en": "Price / 10D MA", "description_zh": "收盘价相对10日均线的位置。", "description_en": "Close price relative to its 10-day moving average."},
    {"name": "MA_RATIO_20", "family": "trend", "label_zh": "20日均线比", "label_en": "Price / 20D MA", "description_zh": "收盘价相对20日均线的位置。", "description_en": "Close price relative to its 20-day moving average."},
    # volume
    {"name": "VMOM_5", "family": "volume", "label_zh": "5日量能比", "label_en": "Volume / 5D avg", "description_zh": "成交量相对5日均量。", "description_en": "Volume relative to 5-day average volume."},
    {"name": "VMOM_10", "family": "volume", "label_zh": "10日量能比", "label_en": "Volume / 10D avg", "description_zh": "成交量相对10日均量。", "description_en": "Volume relative to 10-day average volume."},
    {"name": "VMOM_20", "family": "volume", "label_zh": "20日量能比", "label_en": "Volume / 20D avg", "description_zh": "成交量相对20日均量。", "description_en": "Volume relative to 20-day average volume."},
    {"name": "VSTD_5", "family": "volume", "label_zh": "5日成交量波动", "label_en": "5D volume volatility", "description_zh": "5日成交量标准差 / 均量。", "description_en": "5-day volume standard deviation divided by mean volume."},
    {"name": "VSTD_10", "family": "volume", "label_zh": "10日成交量波动", "label_en": "10D volume volatility", "description_zh": "10日成交量标准差 / 均量。", "description_en": "10-day volume standard deviation divided by mean volume."},
    {"name": "VSTD_20", "family": "volume", "label_zh": "20日成交量波动", "label_en": "20D volume volatility", "description_zh": "20日成交量标准差 / 均量。", "description_en": "20-day volume standard deviation divided by mean volume."},
    # volatility
    {"name": "STD_5", "family": "volatility", "label_zh": "5日价格波动", "label_en": "5D price volatility", "description_zh": "5日收盘价标准差 / 收盘价。", "description_en": "5-day close-price standard deviation divided by close."},
    {"name": "STD_10", "family": "volatility", "label_zh": "10日价格波动", "label_en": "10D price volatility", "description_zh": "10日收盘价标准差 / 收盘价。", "description_en": "10-day close-price standard deviation divided by close."},
    {"name": "STD_20", "family": "volatility", "label_zh": "20日价格波动", "label_en": "20D price volatility", "description_zh": "20日收盘价标准差 / 收盘价。", "description_en": "20-day close-price standard deviation divided by close."},
    {"name": "BBPOS_5", "family": "volatility", "label_zh": "5日布林位置", "label_en": "5D Bollinger position", "description_zh": "价格在5日均线±2倍标准差通道中的位置。", "description_en": "Close location inside the 5-day Bollinger band."},
    {"name": "BBPOS_10", "family": "volatility", "label_zh": "10日布林位置", "label_en": "10D Bollinger position", "description_zh": "价格在10日均线±2倍标准差通道中的位置。", "description_en": "Close location inside the 10-day Bollinger band."},
    {"name": "BBPOS_20", "family": "volatility", "label_zh": "20日布林位置", "label_en": "20D Bollinger position", "description_zh": "价格在20日均线±2倍标准差通道中的位置。", "description_en": "Close location inside the 20-day Bollinger band."},
    # reversion/trend
    {"name": "RSV", "family": "mean_reversion", "label_zh": "9日随机值", "label_en": "9D stochastic RSV", "description_zh": "收盘价在9日高低区间中的百分位。", "description_en": "Close percentile inside the 9-day high-low range."},
    {"name": "RSI_14", "family": "mean_reversion", "label_zh": "14日RSI", "label_en": "14D RSI", "description_zh": "经典超买/超卖相对强弱指标。", "description_en": "Classic relative-strength overbought/oversold oscillator."},
    {"name": "BETA_5", "family": "trend", "label_zh": "5日趋势斜率", "label_en": "5D trend slope", "description_zh": "5日价格滚动回归斜率。", "description_en": "Rolling 5-day price regression slope."},
    {"name": "BETA_10", "family": "trend", "label_zh": "10日趋势斜率", "label_en": "10D trend slope", "description_zh": "10日价格滚动回归斜率。", "description_en": "Rolling 10-day price regression slope."},
    {"name": "BETA_20", "family": "trend", "label_zh": "20日趋势斜率", "label_en": "20D trend slope", "description_zh": "20日价格滚动回归斜率。", "description_en": "Rolling 20-day price regression slope."},
]

_VALID_FACTORS = {f["name"] for f in ALPHA158_FACTORS}
_VALID_TRANSFORMS = {"rank", "zscore"}
_VALID_REBALANCE = {"daily", "weekly", "monthly"}


@dataclass(frozen=True)
class FactorTerm:
    factor: str
    weight: float = 1.0
    transform: str = "rank"


def _round(v: Any, nd: int = 4):
    try:
        if v is None or pd.isna(v) or not math.isfinite(float(v)):
            return None
        return round(float(v), nd)
    except Exception:
        return None


def _market_mask(s: pd.Series, market: str) -> pd.Series:
    is_cn = s.astype(str).str.match(r"^\d{6}\.(SH|SZ)$", na=False)
    return is_cn if market == "CN" else ~is_cn


def _normalise_terms(raw_terms: list[dict[str, Any]]) -> list[FactorTerm]:
    terms: list[FactorTerm] = []
    for item in raw_terms or []:
        factor = str(item.get("factor") or "").strip().upper()
        if factor not in _VALID_FACTORS:
            raise ValueError(f"unsupported Alpha158 factor: {factor or '?'}")
        try:
            weight = float(item.get("weight", 1.0))
        except Exception as exc:
            raise ValueError(f"invalid weight for {factor}") from exc
        if not math.isfinite(weight) or abs(weight) > 100:
            raise ValueError(f"invalid weight for {factor}")
        if abs(weight) < 1e-12:
            continue
        transform = str(item.get("transform") or "rank").strip().lower()
        if transform not in _VALID_TRANSFORMS:
            raise ValueError(f"unsupported transform for {factor}: {transform}")
        terms.append(FactorTerm(factor=factor, weight=weight, transform=transform))
    if not terms:
        raise ValueError("expression must contain at least one non-zero factor term")
    return terms


def _freq_mask(dates: pd.Index, rebalance: str) -> list[str]:
    dates = pd.Index(sorted(pd.to_datetime(dates)))
    if rebalance == "daily":
        return [d.strftime("%Y-%m-%d") for d in dates]
    frame = pd.DataFrame({"date": dates})
    if rebalance == "weekly":
        key = frame["date"].dt.strftime("%G-%V")
    elif rebalance == "monthly":
        key = frame["date"].dt.strftime("%Y-%m")
    else:
        raise ValueError(f"unsupported rebalance: {rebalance}")
    return [d.strftime("%Y-%m-%d") for d in frame.groupby(key, sort=True)["date"].first().tolist()]


def _stats(equity_curve: list[dict[str, Any]], returns: list[float], initial_capital: float) -> dict[str, Any]:
    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_capital
    total_return = final_equity / initial_capital - 1 if initial_capital else 0.0
    days = 0
    if len(equity_curve) >= 2:
        try:
            days = (datetime.fromisoformat(equity_curve[-1]["date"][:10]) - datetime.fromisoformat(equity_curve[0]["date"][:10])).days
        except Exception:
            days = 0
    annualized = ((final_equity / initial_capital) ** (365 / days) - 1) if days > 0 and initial_capital > 0 and final_equity > 0 else 0.0
    peak = -float("inf")
    max_dd = 0.0
    drawdown_curve = []
    for pt in equity_curve:
        eq = float(pt["equity"])
        peak = max(peak, eq)
        dd = eq / peak - 1 if peak > 0 else 0.0
        max_dd = min(max_dd, dd)
        drawdown_curve.append({"date": pt["date"], "drawdown_pct": _round(dd * 100, 4)})
    ret = pd.Series(returns, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = float("nan")
    if len(ret) > 1 and ret.std() > 0:
        # Rebalance-period Sharpe; good enough for lab diagnostics.
        sharpe = float(ret.mean() / ret.std() * math.sqrt(len(ret) / max(days / 365, 1 / 365))) if days > 0 else float(ret.mean() / ret.std() * math.sqrt(252))
    return {
        "final_equity": _round(final_equity, 2),
        "total_return_pct": _round(total_return * 100, 4),
        "annualized_pct": _round(annualized * 100, 4),
        "max_drawdown_pct": _round(max_dd * 100, 4),
        "sharpe": _round(sharpe, 4),
        "n_periods": int(len(ret)),
        "drawdown_curve": drawdown_curve,
    }


def _load_data(con: sqlite3.Connection, terms: list[FactorTerm], market: str, start_date: str, end_date: str):
    names = sorted({t.factor for t in terms})
    placeholders = ",".join(["?"] * len(names))
    fv = pd.read_sql_query(
        f"""
        SELECT ticker, date, factor_name, value
        FROM factor_values
        WHERE factor_group = 'alpha158'
          AND factor_name IN ({placeholders})
          AND date BETWEEN ? AND ?
        ORDER BY date, ticker, factor_name
        """,
        con,
        params=[*names, start_date, end_date],
    )
    if fv.empty:
        raise ValueError("no Alpha158 factor_values in the requested date range")
    fv = fv[_market_mask(fv["ticker"], market)].dropna(subset=["value"]).copy()
    if fv.empty:
        raise ValueError(f"no {market} factor rows in requested date range")

    tickers = sorted(fv["ticker"].unique().tolist())
    placeholders = ",".join(["?"] * len(tickers))
    prices = pd.read_sql_query(
        f"""
        SELECT ticker, datetime, close, volume
        FROM prices
        WHERE interval = '1d'
          AND ticker IN ({placeholders})
          AND date(datetime) BETWEEN date(?, '-120 day') AND date(?, '+90 day')
        ORDER BY ticker, datetime
        """,
        con,
        params=[*tickers, start_date, end_date],
    )
    if prices.empty:
        raise ValueError("no 1d prices for selected factor universe")
    prices["date"] = pd.to_datetime(prices["datetime"]).dt.strftime("%Y-%m-%d")
    prices = prices.dropna(subset=["close"])
    close = prices.pivot_table(index="date", columns="ticker", values="close", aggfunc="last").sort_index()
    volume = prices.pivot_table(index="date", columns="ticker", values="volume", aggfunc="last").sort_index()
    return fv, close, volume


def _select_universe(fv: pd.DataFrame, close: pd.DataFrame, volume: pd.DataFrame, universe_size: int, start_date: str) -> tuple[list[str], pd.Series]:
    candidates = sorted(set(fv["ticker"].unique()).intersection(close.columns))
    if not candidates:
        raise ValueError("factor rows and prices have no ticker overlap")
    # Avoid a subtle look-ahead: prices are loaded beyond end_date so IC can
    # evaluate future returns, but universe selection must not use those future
    # bars. Rank by ADV over the 60 trading days ending at/before start_date.
    hist_close = close.loc[close.index <= start_date, candidates].tail(60)
    hist_volume = volume.reindex(close.index).loc[close.index <= start_date, candidates].tail(60)
    if hist_close.empty or hist_volume.empty:
        hist_close = close.loc[close.index <= start_date, candidates]
        hist_volume = volume.reindex(close.index).loc[close.index <= start_date, candidates]
    dvol = (hist_close * hist_volume).replace([np.inf, -np.inf], np.nan)
    adv = dvol.mean().dropna().sort_values(ascending=False)
    if adv.empty:
        # Fall back to coverage count if volume is missing.
        coverage = fv.groupby("ticker")["date"].nunique().sort_values(ascending=False)
        selected = coverage.index[:universe_size].tolist()
        return selected, coverage
    n = max(10, min(int(universe_size or len(adv)), len(adv)))
    return adv.index[:n].tolist(), adv


def _build_signal(fv: pd.DataFrame, terms: list[FactorTerm]) -> pd.DataFrame:
    pieces = []
    for term in terms:
        part = fv[fv["factor_name"] == term.factor].copy()
        if part.empty:
            continue
        if term.transform == "rank":
            part["x"] = part.groupby("date")["value"].rank(pct=True)
        elif term.transform == "zscore":
            grouped = part.groupby("date")["value"]
            mu = grouped.transform("mean")
            sd = grouped.transform("std").replace(0, np.nan)
            part["x"] = (part["value"] - mu) / sd
            # Convert z-score back to rank-like scale after winsor-ish clipping so
            # one wild value cannot dominate the whole composite.
            part["x"] = part["x"].clip(-5, 5)
        else:  # guarded earlier
            raise ValueError(f"unsupported transform: {term.transform}")
        part["weighted"] = part["x"] * term.weight
        pieces.append(part[["date", "ticker", "weighted"]])
    if not pieces:
        raise ValueError("no usable factor terms after filtering")
    comp = pd.concat(pieces, ignore_index=True).groupby(["date", "ticker"], as_index=False)["weighted"].sum()
    comp["signal"] = comp.groupby("date")["weighted"].rank(pct=True)
    return comp[["date", "ticker", "signal"]]


def _compute_ic(signal: pd.DataFrame, close: pd.DataFrame, horizon: int, window: int) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    future_ret = close.shift(-(horizon + 1)) / close.shift(-1) - 1
    ret_long = future_ret.stack().rename("future_return").reset_index()
    ret_long.columns = ["date", "ticker", "future_return"]
    merged = signal.merge(ret_long, on=["date", "ticker"], how="inner").dropna(subset=["signal", "future_return"])
    rows = []
    qret_acc: dict[int, list[float]] = {i: [] for i in range(1, 6)}
    for dt, g in merged.groupby("date", sort=True):
        if len(g) < 30:
            continue
        ic = g["signal"].rank(pct=True).corr(g["future_return"].rank(pct=True))
        if pd.notna(ic) and math.isfinite(float(ic)):
            rows.append({"date": dt, "ic": float(ic), "n": int(len(g))})
        try:
            q = pd.qcut(g["signal"].rank(method="first"), 5, labels=False) + 1
            g2 = g.assign(q=q)
            for qi, val in g2.groupby("q")["future_return"].mean().items():
                if pd.notna(val):
                    qret_acc[int(qi)].append(float(val))
        except Exception:
            pass
    if not rows:
        return [], {}, []
    ic_df = pd.DataFrame(rows).sort_values("date")
    ic_df["rolling_ic"] = ic_df["ic"].rolling(window, min_periods=max(5, min(window, 10))).mean()
    roll_std = ic_df["ic"].rolling(window, min_periods=max(5, min(window, 10))).std()
    ic_df["rolling_icir"] = ic_df["rolling_ic"] / roll_std.replace(0, np.nan)
    mean_ic = float(ic_df["ic"].mean())
    std_ic = float(ic_df["ic"].std()) if len(ic_df) > 1 else float("nan")
    icir = mean_ic / std_ic if std_ic and math.isfinite(std_ic) and std_ic != 0 else float("nan")
    series = [
        {"date": r["date"], "ic": _round(r["ic"], 5), "rolling_ic": _round(r.get("rolling_ic"), 5), "rolling_icir": _round(r.get("rolling_icir"), 4), "n": int(r["n"])}
        for r in ic_df.replace({np.nan: None}).to_dict(orient="records")
    ]
    summary = {
        "mean_ic": _round(mean_ic, 5),
        "std_ic": _round(std_ic, 5),
        "icir": _round(icir, 4),
        "annualized_icir": _round(icir * math.sqrt(252), 4) if math.isfinite(icir) else None,
        "ic_win_rate": _round(float((ic_df["ic"] > 0).mean()), 4),
        "n_ic_days": int(len(ic_df)),
        "avg_universe_size": int(round(float(ic_df["n"].mean()))),
        "latest_ic": _round(float(ic_df["ic"].iloc[-1]), 5),
        "latest_rolling_icir": _round(float(ic_df["rolling_icir"].dropna().iloc[-1]), 4) if len(ic_df["rolling_icir"].dropna()) else None,
    }
    quantile_returns = [
        {"quantile": q, "mean_forward_return_pct": _round((float(np.mean(vals)) if vals else float("nan")) * 100, 4), "n": len(vals)}
        for q, vals in qret_acc.items()
    ]
    return series, summary, quantile_returns


def _run_portfolio(signal: pd.DataFrame, close: pd.DataFrame, initial_capital: float, top_n: int, cost_bps: float, rebalance: str) -> tuple[list[dict[str, Any]], list[float], float, list[dict[str, Any]], list[dict[str, Any]]]:
    dates = sorted(set(signal["date"]).intersection(close.index))
    rb_dates = _freq_mask(pd.Index(dates), rebalance)
    close_dates = list(close.index)
    close_pos = {d: i for i, d in enumerate(close_dates)}

    equity = float(initial_capital)
    curve = [{"date": rb_dates[0] if rb_dates else (dates[0] if dates else ""), "equity": _round(equity, 2)}]
    period_returns: list[float] = []
    turnovers: list[float] = []
    prev_w = pd.Series(dtype=float)
    trades: list[dict[str, Any]] = []

    sig_by_date = {dt: g.sort_values("signal", ascending=False) for dt, g in signal.groupby("date")}
    for idx, dt in enumerate(rb_dates):
        if dt not in close_pos or dt not in sig_by_date:
            continue
        entry_idx = close_pos[dt] + 1
        next_dt = rb_dates[idx + 1] if idx + 1 < len(rb_dates) else None
        if next_dt and next_dt in close_pos:
            exit_idx = close_pos[next_dt] + 1
        else:
            exit_idx = min(entry_idx + 1, len(close_dates) - 1)
        if entry_idx >= len(close_dates) or exit_idx >= len(close_dates) or exit_idx <= entry_idx:
            continue
        picks = sig_by_date[dt].head(max(1, int(top_n or 20))).copy()
        picks = picks[picks["ticker"].isin(close.columns)]
        if picks.empty:
            continue
        tickers = picks["ticker"].tolist()
        w = pd.Series(1.0 / len(tickers), index=tickers, dtype=float)
        all_idx = sorted(set(prev_w.index).union(w.index))
        turnover = float((w.reindex(all_idx, fill_value=0) - prev_w.reindex(all_idx, fill_value=0)).abs().sum())
        entry_px = close.loc[close_dates[entry_idx], tickers].replace(0, np.nan)
        exit_px = close.loc[close_dates[exit_idx], tickers].replace(0, np.nan)
        sec_ret = (exit_px / entry_px - 1).replace([np.inf, -np.inf], np.nan).dropna()
        if sec_ret.empty:
            continue
        w2 = w.reindex(sec_ret.index)
        gross = float((w2 * sec_ret).sum())
        cost = float(cost_bps) * 1e-4 * turnover
        net = gross - cost
        equity *= (1 + net)
        period_returns.append(net)
        turnovers.append(turnover)
        out_date = close_dates[exit_idx]
        curve.append({"date": out_date, "equity": _round(equity, 2), "period_return_pct": _round(net * 100, 4), "turnover": _round(turnover, 4)})
        for tkr, score in zip(picks["ticker"].head(10), picks["signal"].head(10)):
            trades.append({"signal_date": dt, "entry_date": close_dates[entry_idx], "exit_date": out_date, "ticker": tkr, "score": _round(score, 5)})
        prev_w = w
    avg_turnover = float(np.mean(turnovers)) if turnovers else 0.0

    latest_holdings: list[dict[str, Any]] = []
    if dates:
        latest_dt = dates[-1]
        latest = sig_by_date[latest_dt].head(max(1, int(top_n or 20)))
        latest_holdings = [
            {"ticker": str(r["ticker"]), "score": _round(r["signal"], 5), "weight_pct": _round(100 / max(len(latest), 1), 2)}
            for _, r in latest.iterrows()
        ]
    return curve, period_returns, avg_turnover, latest_holdings, trades[-100:]


def run_factor_lab(request: dict[str, Any]) -> dict[str, Any]:
    market = str(request.get("market") or "US").upper()
    if market not in {"US", "CN"}:
        raise ValueError("market must be US or CN")
    start_date = str(request.get("start_date") or "")[:10]
    end_date = str(request.get("end_date") or "")[:10]
    if not start_date or not end_date:
        raise ValueError("start_date and end_date are required")
    if start_date > end_date:
        raise ValueError("start_date must be <= end_date")
    terms = _normalise_terms(((request.get("expression") or {}).get("terms") or []))
    initial_capital = float(request.get("initial_capital") or (100000 if market == "CN" else 10000))
    universe_size = int(request.get("universe_size") or (300 if market == "CN" else 300))
    top_n = int(request.get("top_n") or 20)
    horizon = int(request.get("horizon") or 5)
    window = int(request.get("window") or 20)
    cost_bps = float(request.get("cost_bps") or 5)
    rebalance = str(request.get("rebalance") or "weekly").lower()
    if rebalance not in _VALID_REBALANCE:
        raise ValueError("rebalance must be daily, weekly, or monthly")
    if not (1 <= horizon <= 60):
        raise ValueError("horizon must be 1..60")
    if not (5 <= window <= 120):
        raise ValueError("window must be 5..120")
    if top_n < 1 or top_n > 500:
        raise ValueError("top_n must be 1..500")
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only = ON")
        fv, close, volume = _load_data(con, terms, market, start_date, end_date)
        selected, ranking = _select_universe(fv, close, volume, universe_size, start_date)
        fv = fv[fv["ticker"].isin(selected)].copy()
        close = close.reindex(columns=selected).dropna(axis=1, how="all")
        if close.empty:
            raise ValueError("no price matrix after universe filtering")
        signal = _build_signal(fv, terms)
        signal = signal[signal["ticker"].isin(close.columns)].copy()
        if signal.empty:
            raise ValueError("no signal after universe filtering")

        ic_series, ic_summary, quantile_returns = _compute_ic(signal, close, horizon, window)
        equity_curve, returns, avg_turnover, latest_holdings, sample_trades = _run_portfolio(signal, close, initial_capital, top_n, cost_bps, rebalance)
        st = _stats(equity_curve, returns, initial_capital)
        warnings: list[str] = [
            "当前股票池使用现有 universe / factor_values 回看历史，不是完整 survivorship-free 数据库。",
            "默认口径避免前瞻：t日信号从 t+1 close 后才开始计收益；IC 使用 close[t+1+h] / close[t+1] - 1。",
            "因子先做横截面 rank/z-score 后组合，避免 RSI/BETA/ROC 等尺度污染。",
        ]
        warnings_en: list[str] = [
            "The universe is reconstructed from current universe / factor_values history; it is not a fully survivorship-free database.",
            "Look-ahead guard: signal at t only earns returns after t+1 close; IC uses close[t+1+h] / close[t+1] - 1.",
            "Each factor is cross-sectionally ranked/z-scored before combination to avoid RSI/BETA/ROC scale pollution.",
        ]
        if (ic_summary.get("n_ic_days") or 0) < 50:
            warnings.append(f"有效 IC 样本仅 {ic_summary.get('n_ic_days') or 0} 天，只能作方向性参考。")
            warnings_en.append(f"Only {ic_summary.get('n_ic_days') or 0} valid IC days; treat this as directional evidence, not a conclusion.")
        if len(equity_curve) <= 2:
            warnings.append("组合回测有效换仓点很少，PnL 曲线统计意义有限。")
            warnings_en.append("Very few effective rebalance points; the PnL curve has limited statistical meaning.")

        summary = {
            **{k: v for k, v in st.items() if k != "drawdown_curve"},
            **ic_summary,
            "avg_turnover": _round(avg_turnover, 4),
            "avg_turnover_pct": _round(avg_turnover * 100, 2),
            "cost_bps": _round(cost_bps, 2),
        }
        return {
            "status": "ok",
            "market": market,
            "expression": {"terms": [t.__dict__ for t in terms], "aggregation": "weighted_transform_then_rank"},
            "summary": summary,
            "equity_curve": equity_curve,
            "drawdown_curve": st["drawdown_curve"],
            "ic_series": ic_series,
            "quantile_returns": quantile_returns,
            "latest_holdings": latest_holdings,
            "sample_trades": sample_trades,
            "coverage": {
                "factor_start": str(fv["date"].min()),
                "factor_end": str(fv["date"].max()),
                "price_start": str(close.index.min()) if len(close.index) else None,
                "price_end": str(close.index.max()) if len(close.index) else None,
                "selected_universe": int(len(selected)),
                "priced_universe": int(len(close.columns)),
                "signal_dates": int(signal["date"].nunique()),
                "ranking_basis": "avg dollar volume" if isinstance(ranking, pd.Series) else "coverage",
            },
            "meta": {
                "start_date": start_date,
                "end_date": end_date,
                "initial_capital": _round(initial_capital, 2),
                "universe_size": int(len(selected)),
                "requested_universe_size": universe_size,
                "top_n": top_n,
                "horizon": horizon,
                "window": window,
                "rebalance": rebalance,
            },
            "warnings": warnings,
            "warnings_en": warnings_en,
        }
    finally:
        con.close()
