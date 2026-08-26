"""Ad-hoc Alpha158 factor laboratory engine.

Account-free research workbench: read 1d OHLCV from the shared trading.db,
compute temporary Alpha158-style factors (including arbitrary lookback periods),
and run look-ahead-safe IC + simplified top-N portfolio diagnostics. It never
creates accounts and never writes to the database.
"""
from __future__ import annotations

import ast
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from core.db import DB_PATH

_QT_ROOT = os.path.expanduser('~/quant-trading')
if _QT_ROOT not in sys.path:
    sys.path.insert(0, _QT_ROOT)

try:
    from config.settings import UNIVERSES as _QT_UNIVERSES
except Exception:
    _QT_UNIVERSES = {}

try:
    from trading.costs import MoomooAUCosts as _MoomooAUCosts, CNCosts as _CNCosts
except Exception:
    _MoomooAUCosts = None
    _CNCosts = None


MARKET_DEFAULTS = {
    "US": {
        "initial_capital": 10000,
        "top_n": 5,
        "rebalance": "daily",
        "rebalance_days": 1,
        "hold_band_mult": 3,
        "cooldown_days": 0,
        "min_hold_days": 0,
    },
    "CN": {
        "initial_capital": 100000,
        "top_n": 5,
        "rebalance": "daily",
        "rebalance_days": 1,
        "hold_band_mult": 3,
        "cooldown_days": 1,
        "min_hold_days": 1,
    },
}


# Catalog is intentionally base-factor oriented. Tunable families (ROC, BETA,
# etc.) accept arbitrary periods such as 7/11, so we do not enumerate only
# Alpha158's historical 5/10/20 variants in the UI.
FACTOR_CATALOG: list[dict[str, Any]] = [
    {"name": "KMID", "kind": "fixed", "family": "kbar", "label_zh": "实体涨跌幅", "label_en": "Candle body return", "description_zh": "收盘价相对开盘价的位置。", "description_en": "Close relative to open."},
    {"name": "KLEN", "kind": "fixed", "family": "kbar", "label_zh": "全振幅", "label_en": "Full candle range", "description_zh": "最高价到最低价相对开盘价的振幅。", "description_en": "High-low range relative to open."},
    {"name": "KMID2", "kind": "fixed", "family": "kbar", "label_zh": "实体/振幅", "label_en": "Body over range", "description_zh": "实体长度占当日高低振幅的比例。", "description_en": "Candle body divided by high-low range."},
    {"name": "KUP", "kind": "fixed", "family": "kbar", "label_zh": "上影线", "label_en": "Upper shadow", "description_zh": "上影线相对开盘价的长度。", "description_en": "Upper shadow relative to open."},
    {"name": "KUP2", "kind": "fixed", "family": "kbar", "label_zh": "上影线/振幅", "label_en": "Upper shadow over range", "description_zh": "上影线占当日振幅比例。", "description_en": "Upper shadow divided by high-low range."},
    {"name": "KLOW", "kind": "fixed", "family": "kbar", "label_zh": "下影线", "label_en": "Lower shadow", "description_zh": "下影线相对开盘价的长度。", "description_en": "Lower shadow relative to open."},
    {"name": "KLOW2", "kind": "fixed", "family": "kbar", "label_zh": "下影线/振幅", "label_en": "Lower shadow over range", "description_zh": "下影线占当日振幅比例。", "description_en": "Lower shadow divided by high-low range."},
    {"name": "KSFT", "kind": "fixed", "family": "kbar", "label_zh": "价格重心偏移", "label_en": "Candle shift", "description_zh": "收盘价相对高低区间中点的偏移。", "description_en": "Close relative to the high-low midpoint."},
    {"name": "KSFT2", "kind": "fixed", "family": "kbar", "label_zh": "价格重心偏移/振幅", "label_en": "Shift over range", "description_zh": "价格重心偏移占当日振幅比例。", "description_en": "Candle shift divided by range."},
    {"name": "ROC", "kind": "tunable", "family": "momentum", "default_period": 20, "period_presets": [1, 5, 7, 10, 11, 20, 60], "label_zh": "N日动量", "label_en": "N-day return momentum", "description_zh": "过去N个交易日涨跌幅；支持任意合理整数周期 1–252，也可输入 5,10,20 合并。", "description_en": "Return over the past N trading days; supports any reasonable integer period 1–252, or merge siblings like 5,10,20."},
    {"name": "MA_RATIO", "kind": "tunable", "family": "trend", "default_period": 20, "period_presets": [3, 5, 7, 10, 11, 20, 60], "label_zh": "价格/N日均线", "label_en": "Price / N-day MA", "description_zh": "收盘价相对N日均线的位置。", "description_en": "Close price relative to its N-day moving average."},
    {"name": "VMOM", "kind": "tunable", "family": "volume", "default_period": 20, "period_presets": [3, 5, 7, 10, 20, 60], "label_zh": "N日量能比", "label_en": "Volume / N-day avg", "description_zh": "成交量相对N日均量。", "description_en": "Volume relative to N-day average volume."},
    {"name": "VSTD", "kind": "tunable", "family": "volume", "default_period": 20, "period_presets": [5, 7, 10, 20, 60], "label_zh": "N日成交量波动", "label_en": "N-day volume volatility", "description_zh": "N日成交量标准差 / 均量。", "description_en": "N-day volume standard deviation divided by mean volume."},
    {"name": "STD", "kind": "tunable", "family": "volatility", "default_period": 20, "period_presets": [5, 7, 10, 20, 60], "label_zh": "N日价格波动", "label_en": "N-day price volatility", "description_zh": "N日收盘价标准差 / 收盘价。", "description_en": "N-day close-price standard deviation divided by close."},
    {"name": "BBPOS", "kind": "tunable", "family": "volatility", "default_period": 20, "period_presets": [5, 7, 10, 20, 60], "label_zh": "N日布林位置", "label_en": "N-day Bollinger position", "description_zh": "价格在N日均线±2倍标准差通道中的位置。", "description_en": "Close location inside the N-day Bollinger band."},
    {"name": "BETA", "kind": "tunable", "family": "trend", "default_period": 20, "period_presets": [5, 7, 10, 11, 20, 60], "label_zh": "N日趋势斜率", "label_en": "N-day trend slope", "description_zh": "N日价格滚动回归斜率。", "description_en": "Rolling N-day price regression slope."},
    {"name": "RSV", "kind": "tunable", "family": "mean_reversion", "default_period": 9, "period_presets": [5, 7, 9, 14, 20], "label_zh": "N日随机值", "label_en": "N-day stochastic RSV", "description_zh": "收盘价在N日高低区间中的百分位。", "description_en": "Close percentile inside the N-day high-low range."},
    {"name": "RSI", "kind": "tunable", "family": "mean_reversion", "default_period": 14, "period_presets": [5, 7, 10, 14, 20], "label_zh": "N日RSI", "label_en": "N-day RSI", "description_zh": "经典超买/超卖相对强弱指标，周期可调。", "description_en": "Classic relative-strength oscillator with tunable period."},
]

ALPHA158_FACTORS = FACTOR_CATALOG  # backwards-compatible import name
_FIXED_FACTORS = {f["name"] for f in FACTOR_CATALOG if f["kind"] == "fixed"}
_TUNABLE_FACTORS = {f["name"] for f in FACTOR_CATALOG if f["kind"] == "tunable"}
_CATALOG_BY_NAME = {f["name"]: f for f in FACTOR_CATALOG}
_VALID_FACTORS = _FIXED_FACTORS | _TUNABLE_FACTORS
_VALID_TRANSFORMS = {"rank", "zscore"}
_VALID_REBALANCE = {"daily", "weekly", "monthly"}
_VALID_DATASET_SCOPES = {"configured", "factor_coverage", "priced"}
_MIN_PERIOD = 1
_MAX_PERIOD = 252
_LATEX_MAX_LEN = 6000
_LATEX_ALLOWED_FUNCS = {
    "rank", "zscore", "rho", "corr", "cov", "delta", "lag", "mean", "sum",
    "std", "min", "max", "max2", "min2", "abs", "log", "sqrt", "sign", "clip",
}
_LATEX_FUNC_ALIASES = {"correlation": "rho"}
_LATEX_FACTOR_SYMBOLS = {
    f["name"]: f["name"] for f in FACTOR_CATALOG
}
_LATEX_FACTOR_SYMBOLS.update({
    "open": "OPEN", "high": "HIGH", "low": "LOW", "close": "CLOSE", "volume": "VOLUME", "vol": "VOLUME",
    "OPEN": "OPEN", "HIGH": "HIGH", "LOW": "LOW", "CLOSE": "CLOSE", "VOLUME": "VOLUME", "VOL": "VOLUME",
})


@dataclass(frozen=True)
class FactorTerm:
    factor: str
    periods: tuple[int, ...]
    weight: float = 1.0
    transform: str = "rank"

    @property
    def display_name(self) -> str:
        if not self.periods:
            return self.factor
        if len(self.periods) == 1:
            return f"{self.factor}_{self.periods[0]}"
        joined = ",".join(str(p) for p in self.periods)
        return f"{self.factor}[{joined}]"


@dataclass(frozen=True)
class FormulaTerm:
    latex: str
    weight: float = 1.0
    transform: str = "rank"

    @property
    def display_name(self) -> str:
        compact = re.sub(r"\s+", " ", self.latex).strip()
        if len(compact) > 80:
            compact = compact[:77] + "…"
        return compact or "LaTeX formula"


ExpressionTerm = FactorTerm | FormulaTerm


@dataclass(frozen=True)
class _FormulaNode:
    kind: str
    value: Any = None
    args: tuple["_FormulaNode", ...] = ()


def _round(v: Any, nd: int = 4):
    try:
        if v is None or pd.isna(v) or not math.isfinite(float(v)):
            return None
        return round(float(v), nd)
    except Exception:
        return None


def _market_predicate_sql(market: str) -> tuple[str, tuple[str]]:
    pred = "ticker GLOB ?" if market == "CN" else "ticker NOT GLOB ?"
    return pred, ('[0-9][0-9][0-9][0-9][0-9][0-9].S[HZ]',)


def _configured_universe(market: str) -> list[str]:
    try:
        out = list((_QT_UNIVERSES or {}).get(market) or [])
        return [str(x) for x in out if x]
    except Exception:
        return []


def _cost_model(market: str):
    if market == "CN" and _CNCosts is not None:
        return _CNCosts()
    if _MoomooAUCosts is not None:
        return _MoomooAUCosts()
    return None


def _estimate_trade_cost_pct(model, side: str, notional: float, price: float, equity: float) -> float:
    if model is None or notional <= 0 or price <= 0 or equity <= 0:
        return 0.0
    try:
        shares = notional / price
        detail = model.calculate(side, shares, price)
        fees = float(detail.get("total_fees") or 0.0)
        slippage = notional * float(getattr(model, "slippage_pct", 0.0) or 0.0)
        return (fees + slippage) / equity
    except Exception:
        slip = float(getattr(model, "slippage_pct", 0.0005) or 0.0005)
        return abs(notional) * slip / equity


def _parse_factor_name(raw: str) -> tuple[str, int | None]:
    s = str(raw or "").strip().upper()
    if s in _VALID_FACTORS:
        return s, None
    for base in sorted(_TUNABLE_FACTORS, key=len, reverse=True):
        prefix = base + "_"
        if s.startswith(prefix):
            tail = s[len(prefix):]
            if tail.isdigit():
                return base, int(tail)
    raise ValueError(f"unsupported factor: {s or '?'}")


def _parse_periods(item: dict[str, Any], base: str, parsed_period: int | None) -> tuple[int, ...]:
    if base in _FIXED_FACTORS:
        return ()
    raw_periods = item.get("periods")
    vals: list[int] = []
    if isinstance(raw_periods, str):
        for part in raw_periods.replace(";", ",").replace(" ", ",").split(","):
            if part.strip():
                vals.append(int(part.strip()))
    elif isinstance(raw_periods, (list, tuple)):
        for x in raw_periods:
            vals.append(int(x))
    elif raw_periods is not None:
        vals.append(int(raw_periods))
    elif item.get("period") is not None:
        vals.append(int(str(item.get("period"))))
    elif parsed_period is not None:
        vals.append(int(parsed_period))
    else:
        vals.append(int(_CATALOG_BY_NAME[base].get("default_period") or 20))

    out: list[int] = []
    for p in vals:
        if not (_MIN_PERIOD <= p <= _MAX_PERIOD):
            raise ValueError(f"period for {base} must be {_MIN_PERIOD}..{_MAX_PERIOD}: {p}")
        if base == "BETA" and p < 2:
            raise ValueError("BETA period must be >= 2")
        if p not in out:
            out.append(p)
    if not out:
        raise ValueError(f"{base} needs at least one period")
    return tuple(out)


def _normalise_terms(raw_terms: list[dict[str, Any]]) -> list[ExpressionTerm]:
    terms: list[ExpressionTerm] = []
    for item in raw_terms or []:
        mode = str(item.get("mode") or item.get("type") or "").strip().lower()
        latex = str(item.get("latex") or "").strip()
        if mode in {"latex", "formula"} or latex:
            if not latex:
                raise ValueError("LaTeX formula term needs a latex field")
            if len(latex) > _LATEX_MAX_LEN:
                raise ValueError(f"LaTeX formula is too long (max {_LATEX_MAX_LEN} chars)")
            try:
                weight = float(item.get("weight", 1.0))
            except Exception as exc:
                raise ValueError("invalid weight for LaTeX formula") from exc
            if not math.isfinite(weight) or abs(weight) > 100:
                raise ValueError("invalid weight for LaTeX formula")
            if abs(weight) < 1e-12:
                continue
            transform = str(item.get("transform") or "rank").strip().lower()
            if transform not in _VALID_TRANSFORMS:
                raise ValueError(f"unsupported transform for LaTeX formula: {transform}")
            # Parse now so validation errors are reported before the expensive
            # OHLCV load. Evaluation reparses from the stored latex string.
            _parse_latex_formula(latex)
            terms.append(FormulaTerm(latex=latex, weight=weight, transform=transform))
            continue

        base, parsed_period = _parse_factor_name(str(item.get("factor") or ""))
        try:
            weight = float(item.get("weight", 1.0))
        except Exception as exc:
            raise ValueError(f"invalid weight for {base}") from exc
        if not math.isfinite(weight) or abs(weight) > 100:
            raise ValueError(f"invalid weight for {base}")
        if abs(weight) < 1e-12:
            continue
        transform = str(item.get("transform") or "rank").strip().lower()
        if transform not in _VALID_TRANSFORMS:
            raise ValueError(f"unsupported transform for {base}: {transform}")
        periods = _parse_periods(item, base, parsed_period)
        terms.append(FactorTerm(factor=base, periods=periods, weight=weight, transform=transform))
    if not terms:
        raise ValueError("expression must contain at least one non-zero factor term")
    return terms


def _latex_base_max_period(latex: str) -> int:
    try:
        node = _parse_latex_formula(latex)
    except Exception:
        return 1
    max_period = 1

    def walk(n: _FormulaNode):
        nonlocal max_period
        if n.kind == "base" and isinstance(n.value, tuple) and n.value[1] is not None:
            try:
                max_period = max(max_period, int(n.value[1]))
            except Exception:
                pass
        for child in n.args:
            walk(child)

    walk(node)
    return max_period


def _max_period(terms: list[ExpressionTerm]) -> int:
    vals: list[int] = []
    for t in terms:
        if isinstance(t, FactorTerm):
            vals.extend(t.periods)
        elif isinstance(t, FormulaTerm):
            vals.append(_latex_base_max_period(t.latex))
    return max(vals or [1])


def _freq_mask(dates: pd.Index, rebalance: str, rebalance_days: int = 1) -> list[str]:
    dates = pd.Index(sorted(pd.to_datetime(dates)))
    if dates.empty:
        return []
    n = max(1, int(rebalance_days or 1))
    if n > 1:
        return [d.strftime("%Y-%m-%d") for d in dates[::n]]
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


def _load_ohlcv(con: sqlite3.Connection, market: str, start_date: str, end_date: str, max_period: int):
    pred, params = _market_predicate_sql(market)
    warmup_days = max(120, max_period * 3 + 10)
    prices = pd.read_sql_query(
        f"""
        SELECT ticker, datetime, open, high, low, close, volume
        FROM prices
        WHERE interval = '1d'
          AND {pred}
          AND date(datetime) BETWEEN date(?, '-{warmup_days} day') AND date(?, '+90 day')
        ORDER BY ticker, datetime
        """,
        con,
        params=[*params, start_date, end_date],
    )
    if prices.empty:
        raise ValueError(f"no {market} 1d OHLCV prices in requested date range")
    prices["date"] = pd.to_datetime(prices["datetime"]).dt.strftime("%Y-%m-%d")
    prices = prices.dropna(subset=["close"])
    matrices = {
        col: prices.pivot_table(index="date", columns="ticker", values=col, aggfunc="last").sort_index()
        for col in ["open", "high", "low", "close", "volume"]
    }
    return matrices


def _factor_coverage_universe(
    con: sqlite3.Connection,
    market: str,
    end_date: str,
    configured: list[str],
    available: set[str],
) -> tuple[list[str], str]:
    """Return tickers that actually had persisted Alpha158 rows by end_date.

    This is the closest Factor Lab can get to the A-account operational data set:
    main.py fetches the configured universe, computes Alpha158, then persists
    the tickers that successfully produced factor rows.  Restricting the lab to
    that latest <= end_date coverage exposes gaps between "configured universe"
    and "real factor pipeline coverage" without depending on arbitrary persisted
    factor values for the lab's tunable formula itself.
    """
    pred, params = _market_predicate_sql(market)
    configured_set = set(configured or [])
    latest = con.execute(
        f"""
        SELECT MAX(date)
        FROM factor_values
        WHERE factor_group='alpha158'
          AND date <= ?
          AND {pred}
        """,
        (end_date, *params),
    ).fetchone()[0]
    if not latest:
        raise ValueError(f"no alpha158 factor_values coverage for {market} on or before {end_date}")
    rows = con.execute(
        f"""
        SELECT DISTINCT ticker
        FROM factor_values
        WHERE factor_group='alpha158'
          AND date=?
          AND {pred}
        ORDER BY ticker
        """,
        (latest, *params),
    ).fetchall()
    tickers = [str(r[0]) for r in rows]
    if configured_set:
        tickers = [t for t in tickers if t in configured_set]
    tickers = [t for t in tickers if t in available]
    if not tickers:
        raise ValueError(f"alpha158 factor coverage exists at {latest}, but none overlap requested prices/universe")
    return tickers, f"actual alpha158 factor coverage ({latest})"


def _select_universe(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    market: str,
    start_date: str,
    *,
    dataset_scope: str = "configured",
    end_date: str | None = None,
    con: sqlite3.Connection | None = None,
) -> tuple[list[str], str]:
    configured = _configured_universe(market)
    available = set(close.columns.tolist())
    scope = (dataset_scope or "configured").strip().lower()
    if scope not in _VALID_DATASET_SCOPES:
        raise ValueError("dataset_scope must be configured, factor_coverage, or priced")

    if scope == "factor_coverage":
        if con is None:
            raise ValueError("factor_coverage scope needs a database connection")
        return _factor_coverage_universe(con, market, end_date or start_date, configured, available)

    if scope == "configured" and configured:
        selected = [t for t in configured if t in available]
        if selected:
            return selected, "configured live account universe"
    candidates = sorted(available)
    if not candidates:
        raise ValueError("price matrix has no tickers")
    # Fallback only: if settings universe is unavailable, use pre-start ADV.
    # Avoid look-ahead: prices are loaded beyond end_date for forward-return
    # evaluation, but universe selection uses only bars at/before start_date.
    hist_close = close.loc[close.index <= start_date, candidates].tail(60)
    hist_volume = volume.reindex(close.index).loc[close.index <= start_date, candidates].tail(60)
    dvol = (hist_close * hist_volume).replace([np.inf, -np.inf], np.nan)
    adv = dvol.mean().dropna().sort_values(ascending=False)
    if adv.empty:
        coverage = close.loc[close.index <= start_date].notna().sum().sort_values(ascending=False)
        return coverage.index.tolist(), "coverage fallback"
    return adv.index.tolist(), "pre-start avg dollar volume fallback"


def _rolling_slope(close: pd.DataFrame, period: int) -> pd.DataFrame:
    def slope(y):
        if len(y) < period or np.isnan(y).any():
            return np.nan
        x = np.arange(len(y), dtype=float)
        x -= x.mean()
        yy = y - y.mean()
        denom = (x * x).sum()
        if denom == 0:
            return 0.0
        return (x * yy).sum() / denom / (y.mean() + 1e-12)
    return close.rolling(period).apply(slope, raw=True)


def _rsi(close: pd.DataFrame, period: int) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-12)
    return 100 - 100 / (1 + rs)


def _compute_factor_matrix(base: str, period: int | None, m: dict[str, pd.DataFrame]) -> pd.DataFrame:
    openp, high, low, close, volume = m["open"], m["high"], m["low"], m["close"], m["volume"]
    if base in {"OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"}:
        return {"OPEN": openp, "HIGH": high, "LOW": low, "CLOSE": close, "VOLUME": volume}[base]
    if base == "KMID":
        return (close - openp) / openp
    if base == "KLEN":
        return (high - low) / openp
    if base == "KMID2":
        return (close - openp) / (high - low + 1e-12)
    if base == "KUP":
        return (high - pd.concat([openp.stack(), close.stack()], axis=1).max(axis=1).unstack()) / openp
    if base == "KUP2":
        return (high - pd.concat([openp.stack(), close.stack()], axis=1).max(axis=1).unstack()) / (high - low + 1e-12)
    if base == "KLOW":
        return (pd.concat([openp.stack(), close.stack()], axis=1).min(axis=1).unstack() - low) / openp
    if base == "KLOW2":
        return (pd.concat([openp.stack(), close.stack()], axis=1).min(axis=1).unstack() - low) / (high - low + 1e-12)
    if base == "KSFT":
        return (2 * close - high - low) / openp
    if base == "KSFT2":
        return (2 * close - high - low) / (high - low + 1e-12)
    if period is None:
        raise ValueError(f"{base} requires a period")
    if base == "ROC":
        return close.pct_change(period)
    if base == "MA_RATIO":
        return close / close.rolling(period).mean()
    if base == "VMOM":
        return volume / (volume.rolling(period).mean() + 1e-12)
    if base == "VSTD":
        return volume.rolling(period).std() / (volume.rolling(period).mean() + 1e-12)
    if base == "STD":
        return close.rolling(period).std() / close
    if base == "BBPOS":
        ma = close.rolling(period).mean()
        std = close.rolling(period).std()
        return (close - ma) / (2 * std + 1e-12)
    if base == "BETA":
        return _rolling_slope(close, period)
    if base == "RSV":
        low_min = low.rolling(period).min()
        high_max = high.rolling(period).max()
        return (close - low_min) / (high_max - low_min + 1e-12)
    if base == "RSI":
        return _rsi(close, period)
    raise ValueError(f"unsupported factor: {base}")


def _transform_long(df: pd.DataFrame, transform: str) -> pd.DataFrame:
    long = df.stack().rename("value").reset_index()
    long.columns = ["date", "ticker", "value"]
    long = long.dropna(subset=["value"])
    if long.empty:
        return long.assign(x=pd.Series(dtype=float))
    if transform == "rank":
        long["x"] = long.groupby("date")["value"].rank(pct=True)
    elif transform == "zscore":
        grouped = long.groupby("date")["value"]
        mu = grouped.transform("mean")
        sd = grouped.transform("std").replace(0, np.nan)
        long["x"] = ((long["value"] - mu) / sd).clip(-5, 5)
    else:
        raise ValueError(f"unsupported transform: {transform}")
    return long[["date", "ticker", "x"]]


def _transform_matrix(df: pd.DataFrame, transform: str) -> pd.DataFrame:
    if transform == "rank":
        return df.rank(axis=1, pct=True)
    if transform == "zscore":
        mu = df.mean(axis=1)
        sd = df.std(axis=1).replace(0, np.nan)
        return df.sub(mu, axis=0).div(sd, axis=0).clip(-5, 5)
    raise ValueError(f"unsupported transform: {transform}")


def _extract_braced(s: str, open_idx: int) -> tuple[str, int]:
    if open_idx >= len(s) or s[open_idx] != "{":
        raise ValueError("invalid LaTeX command: expected {...}")
    depth = 0
    start = open_idx + 1
    for i in range(open_idx, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
    raise ValueError("unbalanced braces in LaTeX formula")


def _replace_braced_command(s: str, command: str, nargs: int, repl) -> str:
    needle = "\\" + command
    while True:
        i = s.find(needle)
        if i < 0:
            return s
        pos = i + len(needle)
        args = []
        for _ in range(nargs):
            while pos < len(s) and s[pos].isspace():
                pos += 1
            if pos >= len(s) or s[pos] != "{":
                break
            arg, pos = _extract_braced(s, pos)
            args.append(arg)
        if len(args) != nargs:
            # Not the command form we handle; leave it for the generic command
            # mapper to produce a readable validation error if needed.
            return s
        s = s[:i] + repl(*args) + s[pos:]


def _normalise_latex_to_expr(latex: str) -> str:
    s = str(latex or "").strip()
    # Friendly typo support from mobile keyboards / chat: /rho_5 -> \rho_5.
    s = re.sub(r"/(?=(?:rho|corr|cov|Delta|delta|rank|zscore|sqrt|log|frac)(?:\b|_))", r"\\", s)
    if s.startswith("$$") and s.endswith("$$"):
        s = s[2:-2]
    elif s.startswith("$") and s.endswith("$"):
        s = s[1:-1]
    s = s.replace("\\[", "").replace("\\]", "").replace("\\(", "").replace("\\)", "")
    s = s.replace("ρ", "rho")
    s = s.replace("\\left", "").replace("\\right", "")
    s = re.sub(r"\\operatorname\s*\{([^{}]+)\}", r"\1", s)
    s = re.sub(r"\\(?:mathrm|mathbf|mathit|text)\s*\{([^{}]+)\}", r"\1", s)
    s = _replace_braced_command(s, "frac", 2, lambda a, b: f"(({_normalise_latex_to_expr(a)})/({_normalise_latex_to_expr(b)}))")
    s = _replace_braced_command(s, "sqrt", 1, lambda a: f"sqrt({_normalise_latex_to_expr(a)})")
    s = _replace_braced_command(s, "log", 1, lambda a: f"log({_normalise_latex_to_expr(a)})")
    # Commands with optional numeric subscript: \rho_5, \rho_{5}, \Delta_{10}.
    def cmd_sub(m):
        cmd = _LATEX_FUNC_ALIASES.get(m.group(1), m.group(1))
        return f"{cmd}_{m.group(2)}"
    s = re.sub(r"\\([A-Za-z]+)_\{(\d+)\}", cmd_sub, s)
    s = re.sub(r"\\([A-Za-z]+)_(\d+)", cmd_sub, s)
    def cmd_plain(m):
        cmd = _LATEX_FUNC_ALIASES.get(m.group(1), m.group(1)) or m.group(1)
        if cmd == "Delta":
            cmd = "delta"
        return str(cmd)
    s = re.sub(r"\\([A-Za-z]+)", cmd_plain, s)
    # Factor/fn subscripts: ROC_{5}, CLOSE_{t} (the latter is intentionally
    # not supported as a time index, but this keeps numeric factor syntax nice).
    s = re.sub(r"([A-Za-z][A-Za-z0-9]*)_\{(\d+)\}", r"\1_\2", s)
    s = re.sub(r"\^\{([^{}]+)\}", r"**(\1)", s)
    s = re.sub(r"\^(\d+(?:\.\d+)?)", r"**\1", s)
    s = s.replace("\u2212", "-").replace("×", "*").replace("·", "*")
    s = re.sub(r"\\[,;! ]", "", s)
    s = re.sub(r"(\d+(?:\.\d+)?|\))\s+(?=[A-Za-z_(])", r"\1*", s)
    return s.strip()


def _parse_latex_formula(latex: str) -> _FormulaNode:
    expr = _normalise_latex_to_expr(latex)
    if not expr:
        raise ValueError("empty LaTeX formula")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"unsupported LaTeX formula syntax near: {expr[:120]}") from exc

    def parse_node(node: ast.AST) -> _FormulaNode:
        if isinstance(node, ast.Expression):
            return parse_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            v = float(node.value)
            if not math.isfinite(v):
                raise ValueError("non-finite number in LaTeX formula")
            return _FormulaNode("number", v)
        if isinstance(node, ast.Name):
            return _FormulaNode("base", _parse_formula_symbol(node.id))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            op = "neg" if isinstance(node.op, ast.USub) else "pos"
            return _FormulaNode("unary", op, (parse_node(node.operand),))
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            op = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div", ast.Pow: "pow"}[type(node.op)]
            return _FormulaNode("op", op, (parse_node(node.left), parse_node(node.right)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fname = node.func.id
            base, period = _split_func_period(fname)
            if base not in _LATEX_ALLOWED_FUNCS:
                raise ValueError(f"unsupported LaTeX function: {fname}")
            if node.keywords:
                raise ValueError("keyword arguments are not supported in LaTeX formulas")
            return _FormulaNode("call", (base, period), tuple(parse_node(a) for a in node.args))
        raise ValueError("unsupported LaTeX formula syntax; use arithmetic, factor names, and whitelisted functions only")

    return parse_node(tree)


def _parse_formula_symbol(name: str) -> tuple[str, int | None]:
    raw = str(name or "").strip()
    if raw in _LATEX_FACTOR_SYMBOLS:
        mapped = _LATEX_FACTOR_SYMBOLS[raw]
        if mapped in {"OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"}:
            return mapped, None
        return _parse_factor_name(mapped)
    upper = raw.upper()
    if upper in _LATEX_FACTOR_SYMBOLS:
        mapped = _LATEX_FACTOR_SYMBOLS[upper]
        if mapped in {"OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"}:
            return mapped, None
        return _parse_factor_name(mapped)
    try:
        return _parse_factor_name(upper)
    except Exception as exc:
        raise ValueError(f"unsupported symbol in LaTeX formula: {raw}") from exc


def _split_func_period(name: str) -> tuple[str, int | None]:
    raw = str(name or "").strip()
    m = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*?)_(\d+)", raw)
    if m:
        base = _LATEX_FUNC_ALIASES.get(m.group(1), m.group(1)).lower()
        if base == "delta":
            base = "delta"
        p = int(m.group(2))
        if not (_MIN_PERIOD <= p <= _MAX_PERIOD):
            raise ValueError(f"function period must be {_MIN_PERIOD}..{_MAX_PERIOD}: {p}")
        return base, p
    base = _LATEX_FUNC_ALIASES.get(raw, raw).lower()
    if base == "delta":
        base = "delta"
    return base, None


def _number_node_value(node: _FormulaNode) -> float | None:
    if node.kind == "number":
        return float(node.value)
    if node.kind == "unary" and node.args and node.args[0].kind == "number":
        v = float(node.args[0].value)
        return -v if node.value == "neg" else v
    return None


def _period_from_call(func: str, period: int | None, args: tuple[_FormulaNode, ...], pos: int, default: int | None = None) -> int:
    p = period
    if p is None and len(args) > pos:
        v = _number_node_value(args[pos])
        if v is not None and float(v).is_integer():
            p = int(v)
    if p is None:
        if default is None:
            raise ValueError(f"{func} needs a numeric period, e.g. {func}_5(...)")
        p = default
    if not (_MIN_PERIOD <= int(p) <= _MAX_PERIOD):
        raise ValueError(f"{func} period must be {_MIN_PERIOD}..{_MAX_PERIOD}: {p}")
    return int(p)


def _eval_formula_node(node: _FormulaNode, matrices: dict[str, pd.DataFrame]):
    if node.kind == "number":
        return float(node.value)
    if node.kind == "base":
        base, period = node.value
        if base in _TUNABLE_FACTORS and period is None:
            period = int(_CATALOG_BY_NAME[base].get("default_period") or 20)
        return _compute_factor_matrix(base, period, matrices)
    if node.kind == "unary":
        v = _eval_formula_node(node.args[0], matrices)
        return -v if node.value == "neg" else v
    if node.kind == "op":
        a = _eval_formula_node(node.args[0], matrices)
        b = _eval_formula_node(node.args[1], matrices)
        if node.value == "add":
            return a + b
        if node.value == "sub":
            return a - b
        if node.value == "mul":
            return a * b
        if node.value == "div":
            return a / (b + 1e-12 if isinstance(b, pd.DataFrame) else b)
        if node.value == "pow":
            return a ** b
    if node.kind == "call":
        func, period = node.value
        args = node.args
        if func in {"rank", "zscore"}:
            if len(args) != 1:
                raise ValueError(f"{func}() takes one argument")
            return _transform_matrix(_eval_formula_node(args[0], matrices), func)
        if func in {"rho", "corr"}:
            if len(args) not in {2, 3}:
                raise ValueError("rho/corr takes two series plus optional period")
            p = _period_from_call("rho", period, args, 2)
            a = _eval_formula_node(args[0], matrices)
            b = _eval_formula_node(args[1], matrices)
            return a.rolling(p).corr(b)
        if func == "cov":
            if len(args) not in {2, 3}:
                raise ValueError("cov takes two series plus optional period")
            p = _period_from_call("cov", period, args, 2)
            return _eval_formula_node(args[0], matrices).rolling(p).cov(_eval_formula_node(args[1], matrices))
        if func in {"delta", "lag", "mean", "sum", "std", "min", "max"}:
            if len(args) not in {1, 2}:
                raise ValueError(f"{func} takes one series plus optional period")
            p = _period_from_call(func, period, args, 1)
            x = _eval_formula_node(args[0], matrices)
            if func == "delta":
                return x - x.shift(p)
            if func == "lag":
                return x.shift(p)
            if func == "mean":
                return x.rolling(p).mean()
            if func == "sum":
                return x.rolling(p).sum()
            if func == "std":
                return x.rolling(p).std()
            if func == "min":
                return x.rolling(p).min()
            if func == "max":
                return x.rolling(p).max()
        if func in {"abs", "log", "sqrt", "sign"}:
            if len(args) != 1:
                raise ValueError(f"{func} takes one argument")
            x = _eval_formula_node(args[0], matrices)
            if func == "abs":
                return x.abs() if isinstance(x, pd.DataFrame) else abs(x)
            if func == "log":
                return np.log(x.replace(0, np.nan).abs() + 1e-12) if isinstance(x, pd.DataFrame) else math.log(abs(float(x)) + 1e-12)
            if func == "sqrt":
                return np.sqrt(x.clip(lower=0)) if isinstance(x, pd.DataFrame) else math.sqrt(max(float(x), 0.0))
            if func == "sign":
                return np.sign(x)
        if func in {"max2", "min2"}:
            if len(args) != 2:
                raise ValueError(f"{func} takes two arguments")
            left = _eval_formula_node(args[0], matrices)
            right = _eval_formula_node(args[1], matrices)
            op = np.maximum if func == "max2" else np.minimum
            return op(left, right)
        if func == "clip":
            if len(args) != 3:
                raise ValueError("clip takes x, low, high")
            x = _eval_formula_node(args[0], matrices)
            lo = _number_node_value(args[1])
            hi = _number_node_value(args[2])
            if lo is None or hi is None:
                raise ValueError("clip bounds must be numeric constants")
            return x.clip(lower=lo, upper=hi) if isinstance(x, pd.DataFrame) else min(max(float(x), lo), hi)
    raise ValueError("could not evaluate LaTeX formula")


def _compute_formula_matrix(latex: str, matrices: dict[str, pd.DataFrame]) -> pd.DataFrame:
    node = _parse_latex_formula(latex)
    out = _eval_formula_node(node, matrices)
    if not isinstance(out, pd.DataFrame):
        raise ValueError("LaTeX formula must evaluate to a ticker×date matrix, not a scalar")
    return out.replace([np.inf, -np.inf], np.nan)


def _build_signal(matrices: dict[str, pd.DataFrame], terms: list[ExpressionTerm], start_date: str, end_date: str) -> pd.DataFrame:
    pieces = []
    for term in terms:
        period_parts = []
        if isinstance(term, FactorTerm):
            for period in (term.periods or (None,)):
                mat = _compute_factor_matrix(term.factor, period, matrices)
                mat = mat.loc[(mat.index >= start_date) & (mat.index <= end_date)]
                long = _transform_long(mat, term.transform)
                if not long.empty:
                    period_parts.append(long)
        elif isinstance(term, FormulaTerm):
            mat = _compute_formula_matrix(term.latex, matrices)
            mat = mat.loc[(mat.index >= start_date) & (mat.index <= end_date)]
            long = _transform_long(mat, term.transform)
            if not long.empty:
                period_parts.append(long)
        if not period_parts:
            continue
        merged_periods = pd.concat(period_parts, ignore_index=True).groupby(["date", "ticker"], as_index=False)["x"].mean()
        merged_periods["weighted"] = merged_periods["x"] * term.weight
        pieces.append(merged_periods[["date", "ticker", "weighted"]])
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


def _run_portfolio(
    signal: pd.DataFrame,
    close: pd.DataFrame,
    initial_capital: float,
    top_n: int,
    rebalance: str,
    rebalance_days: int,
    hold_band_mult: int,
    cooldown_days: int,
    min_hold_days: int,
    market: str,
) -> tuple[list[dict[str, Any]], list[float], float, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    dates = sorted(set(signal["date"]).intersection(close.index))
    rb_dates = _freq_mask(pd.Index(dates), rebalance, rebalance_days)
    close_dates = list(close.index)
    close_pos = {d: i for i, d in enumerate(close_dates)}

    equity = float(initial_capital)
    curve = [{"date": rb_dates[0] if rb_dates else (dates[0] if dates else ""), "equity": _round(equity, 2)}]
    period_returns: list[float] = []
    turnovers: list[float] = []
    prev_w = pd.Series(dtype=float)
    buy_dates: dict[str, str] = {}
    cooldown_until: dict[str, str] = {}
    trades: list[dict[str, Any]] = []
    cost_model = _cost_model(market)
    cost_pcts: list[float] = []
    skipped_cooldown = 0
    skipped_min_hold = 0
    held_by_hysteresis = 0

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

        raw_picks = sig_by_date[dt][sig_by_date[dt]["ticker"].isin(close.columns)].copy()
        ranked_tickers = raw_picks["ticker"].tolist()
        top_n_i = max(1, int(top_n or 20))
        hold_mult_i = max(1, int(hold_band_mult or 1))
        hold_n = top_n_i * hold_mult_i
        buy_band = set(ranked_tickers[:top_n_i])
        hold_band = set(ranked_tickers[:hold_n])

        # Rank-cutoff hysteresis / no-trade band:
        # new buys must pass the strict top-N buy band, but existing holdings
        # are not sold until they fall outside top_N * hold_band_mult.
        retained = [tkr for tkr in ranked_tickers if tkr in prev_w.index and tkr in hold_band]
        held_by_hysteresis += sum(1 for tkr in retained if tkr not in buy_band)
        allowed = list(retained)
        for tkr in ranked_tickers:
            if len(allowed) >= top_n_i:
                break
            if tkr in allowed:
                continue
            if cooldown_until.get(tkr) and dt <= cooldown_until[tkr]:
                skipped_cooldown += 1
                continue
            allowed.append(tkr)
        target = pd.Series(1.0 / len(allowed), index=allowed, dtype=float) if allowed else pd.Series(dtype=float)

        if min_hold_days > 0 and len(prev_w):
            for tkr in prev_w.index:
                if tkr not in target.index:
                    bdt = buy_dates.get(tkr)
                    if bdt:
                        try:
                            age = (pd.Timestamp(dt) - pd.Timestamp(bdt)).days
                        except Exception:
                            age = min_hold_days
                        if age < min_hold_days:
                            target.loc[tkr] = prev_w.loc[tkr]
                            skipped_min_hold += 1
        if len(target):
            target = target / target.sum()

        all_idx = sorted(set(prev_w.index).union(target.index))
        turnover = float((target.reindex(all_idx, fill_value=0) - prev_w.reindex(all_idx, fill_value=0)).abs().sum()) if all_idx else 0.0
        entry_date = close_dates[entry_idx]
        exit_date = close_dates[exit_idx]
        if target.empty:
            continue
        entry_px = close.loc[entry_date, target.index].replace(0, np.nan)
        exit_px = close.loc[exit_date, target.index].replace(0, np.nan)
        sec_ret = (exit_px / entry_px - 1).replace([np.inf, -np.inf], np.nan).dropna()
        if sec_ret.empty:
            continue
        target = target.reindex(sec_ret.index).dropna()
        target = target / target.sum()
        gross = float((target * sec_ret).sum())

        cost_pct = 0.0
        prev_aligned = prev_w.reindex(sorted(set(prev_w.index).union(target.index)), fill_value=0)
        target_aligned = target.reindex(prev_aligned.index, fill_value=0)
        delta = target_aligned - prev_aligned
        for tkr, dw in delta.items():
            if abs(dw) < 1e-12:
                continue
            px = float(entry_px.get(tkr, np.nan)) if tkr in entry_px.index else np.nan
            if not math.isfinite(px) or px <= 0:
                px = float(close.loc[entry_date, tkr]) if tkr in close.columns else np.nan
            if not math.isfinite(px) or px <= 0:
                continue
            side = "buy" if dw > 0 else "sell"
            cost_pct += _estimate_trade_cost_pct(cost_model, side, abs(dw) * equity, px, equity)
        cost_pcts.append(cost_pct)
        net = gross - cost_pct
        equity *= (1 + net)
        period_returns.append(net)
        turnovers.append(turnover)
        curve.append({"date": exit_date, "equity": _round(equity, 2), "period_return_pct": _round(net * 100, 4), "turnover": _round(turnover, 4), "cost_pct": _round(cost_pct * 100, 4)})

        sold = set(prev_w.index) - set(target.index)
        for tkr in sold:
            buy_dates.pop(tkr, None)
            if cooldown_days > 0:
                try:
                    cooldown_until[tkr] = close_dates[min(len(close_dates) - 1, close_pos[dt] + cooldown_days)]
                except Exception:
                    cooldown_until[tkr] = dt
        for tkr in target.index:
            if tkr not in prev_w.index:
                buy_dates[tkr] = dt
        ranked = sig_by_date[dt].set_index("ticker")
        for tkr in target.index[:10]:
            trades.append({"signal_date": dt, "entry_date": entry_date, "exit_date": exit_date, "ticker": str(tkr), "score": _round(ranked.loc[tkr, "signal"] if tkr in ranked.index else None, 5)})
        prev_w = target
    avg_turnover = float(np.mean(turnovers)) if turnovers else 0.0

    latest_holdings: list[dict[str, Any]] = []
    if dates:
        latest_dt = dates[-1]
        latest = sig_by_date[latest_dt].head(max(1, int(top_n or 20)))
        latest_holdings = [
            {"ticker": str(r["ticker"]), "score": _round(r["signal"], 5), "weight_pct": _round(100 / max(len(latest), 1), 2)}
            for _, r in latest.iterrows()
        ]
    diagnostics = {
        "avg_cost_pct": _round(float(np.mean(cost_pcts)) * 100, 4) if cost_pcts else 0,
        "skipped_cooldown": skipped_cooldown,
        "skipped_min_hold": skipped_min_hold,
        "held_by_hysteresis": held_by_hysteresis,
        "hold_band_mult": max(1, int(hold_band_mult or 1)),
        "hold_threshold": max(1, int(top_n or 20)) * max(1, int(hold_band_mult or 1)),
        "cost_model": "CNCosts" if market == "CN" else "MoomooAUCosts",
    }
    return curve, period_returns, avg_turnover, latest_holdings, trades[-100:], diagnostics


def _term_payload(t: ExpressionTerm) -> dict[str, Any]:
    base = {"weight": t.weight, "transform": t.transform, "display_name": t.display_name}
    if isinstance(t, FactorTerm):
        return {**base, "mode": "factor", "factor": t.factor, "periods": list(t.periods)}
    return {**base, "mode": "latex", "latex": t.latex}


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
    defaults = MARKET_DEFAULTS[market]
    initial_capital = float(request.get("initial_capital") or defaults["initial_capital"])
    top_n = int(request.get("top_n") or defaults["top_n"])
    horizon = int(request.get("horizon") or 5)
    window = int(request.get("window") or 20)
    rebalance_days = int(request.get("rebalance_days") or defaults["rebalance_days"])
    hold_band_mult = int(request.get("hold_band_mult") or defaults["hold_band_mult"])
    cooldown_days = int(request.get("cooldown_days") or defaults["cooldown_days"])
    min_hold_days = int(request.get("min_hold_days") or defaults["min_hold_days"])
    rebalance = str(request.get("rebalance") or defaults["rebalance"]).lower()
    dataset_scope = str(request.get("dataset_scope") or "configured").strip().lower()
    if rebalance not in _VALID_REBALANCE:
        raise ValueError("rebalance must be daily, weekly, or monthly")
    if dataset_scope not in _VALID_DATASET_SCOPES:
        raise ValueError("dataset_scope must be configured, factor_coverage, or priced")
    if not (1 <= horizon <= 60):
        raise ValueError("horizon must be 1..60")
    if not (5 <= window <= 120):
        raise ValueError("window must be 5..120")
    if top_n < 1 or top_n > 500:
        raise ValueError("top_n must be 1..500")
    if not (1 <= rebalance_days <= 60):
        raise ValueError("rebalance_days must be 1..60")
    if not (1 <= hold_band_mult <= 10):
        raise ValueError("hold_band_mult must be 1..10")
    if not (0 <= cooldown_days <= 60):
        raise ValueError("cooldown_days must be 0..60")
    if not (0 <= min_hold_days <= 60):
        raise ValueError("min_hold_days must be 0..60")
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only = ON")
        matrices = _load_ohlcv(con, market, start_date, end_date, _max_period(terms))
        selected, ranking_basis = _select_universe(
            matrices["close"], matrices["volume"], market, start_date,
            dataset_scope=dataset_scope, end_date=end_date, con=con,
        )
        matrices = {k: v.reindex(columns=selected).dropna(axis=1, how="all") for k, v in matrices.items()}
        close = matrices["close"]
        if close.empty:
            raise ValueError("no price matrix after universe filtering")
        signal = _build_signal(matrices, terms, start_date, end_date)
        signal = signal[signal["ticker"].isin(close.columns)].copy()
        if signal.empty:
            raise ValueError("no signal after universe filtering")

        ic_series, ic_summary, quantile_returns = _compute_ic(signal, close, horizon, window)
        equity_curve, returns, avg_turnover, latest_holdings, sample_trades, exec_diag = _run_portfolio(
            signal, close, initial_capital, top_n, rebalance, rebalance_days,
            hold_band_mult, cooldown_days, min_hold_days, market,
        )
        st = _stats(equity_curve, returns, initial_capital)
        merged_terms = [t for t in terms if isinstance(t, FactorTerm) and len(t.periods) > 1]
        formula_terms = [t for t in terms if isinstance(t, FormulaTerm)]
        warnings: list[str] = [
            "当前股票池使用现有 universe / prices 回看历史，不是完整 survivorship-free 数据库。",
            "默认口径避免前瞻：t日信号从 t+1 close 后才开始计收益；IC 使用 close[t+1+h] / close[t+1] - 1。",
            "因子先做横截面 rank/z-score 后组合，避免 RSI/BETA/ROC 等尺度污染。",
            "可调周期因子按日线 OHLCV 临时计算；输入 5,10,20 会先合并同族周期，再参与总表达式。",
            f"数据口径：{ranking_basis}（可用 {len(selected)} 支）；交易成本使用 {exec_diag.get('cost_model')} 估算。",
            f"已应用 rank-cutoff hysteresis / no-trade band：买入阈值 top {top_n}，持有阈值 top {top_n * hold_band_mult}。",
        ]
        warnings_en: list[str] = [
            "The universe is reconstructed from current universe / prices history; it is not a fully survivorship-free database.",
            "Look-ahead guard: signal at t only earns returns after t+1 close; IC uses close[t+1+h] / close[t+1] - 1.",
            "Each factor is cross-sectionally ranked/z-scored before combination to avoid RSI/BETA/ROC scale pollution.",
            "Tunable-period factors are computed on the fly from 1d OHLCV; entering 5,10,20 merges those sibling periods before the total expression.",
            f"Dataset scope: {ranking_basis} ({len(selected)} usable tickers); transaction cost estimate uses {exec_diag.get('cost_model')}.",
            f"Rank-cutoff hysteresis / no-trade band is applied: buy threshold top {top_n}, hold threshold top {top_n * hold_band_mult}.",
        ]
        if formula_terms:
            warnings.append("LaTeX 公式会先被解析成白名单计算图（如 \\rho_N、rolling mean/std、rank/zscore），不会执行任意 Python/JS。")
            warnings_en.append("LaTeX formulas are parsed into a whitelisted compute graph (e.g. \\rho_N, rolling mean/std, rank/zscore); arbitrary Python/JS is never executed.")
        if merged_terms:
            names = ", ".join(t.display_name for t in merged_terms[:4])
            warnings.append(f"已合并同族周期：{names}。")
            warnings_en.append(f"Merged sibling periods inside terms: {names}.")
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
            "avg_cost_pct": exec_diag.get("avg_cost_pct", 0),
            "skipped_cooldown": exec_diag.get("skipped_cooldown", 0),
            "skipped_min_hold": exec_diag.get("skipped_min_hold", 0),
            "held_by_hysteresis": exec_diag.get("held_by_hysteresis", 0),
        }
        return {
            "status": "ok",
            "market": market,
            "expression": {
                "terms": [_term_payload(t) for t in terms],
                "aggregation": "per_period_transform_then_merge_periods_then_weighted_rank",
            },
            "summary": summary,
            "equity_curve": equity_curve,
            "drawdown_curve": st["drawdown_curve"],
            "ic_series": ic_series,
            "quantile_returns": quantile_returns,
            "latest_holdings": latest_holdings,
            "sample_trades": sample_trades,
            "coverage": {
                "price_start": str(close.index.min()) if len(close.index) else None,
                "price_end": str(close.index.max()) if len(close.index) else None,
                "selected_universe": int(len(selected)),
                "priced_universe": int(len(close.columns)),
                "signal_dates": int(signal["date"].nunique()),
                "ranking_basis": ranking_basis,
                "dataset_scope": dataset_scope,
                "max_factor_period": _max_period(terms),
            },
            "meta": {
                "start_date": start_date,
                "end_date": end_date,
                "initial_capital": _round(initial_capital, 2),
                "universe_size": int(len(selected)),
                "top_n": top_n,
                "horizon": horizon,
                "window": window,
                "rebalance": rebalance,
                "dataset_scope": dataset_scope,
                "rebalance_days": rebalance_days,
                "hold_band_mult": hold_band_mult,
                "hold_threshold": top_n * hold_band_mult,
                "cooldown_days": cooldown_days,
                "min_hold_days": min_hold_days,
                "cost_model": exec_diag.get("cost_model"),
            },
            "warnings": warnings,
            "warnings_en": warnings_en,
        }
    finally:
        con.close()
