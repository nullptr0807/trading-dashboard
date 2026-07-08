from __future__ import annotations

import asyncio
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.backtest import _validate_market
from core.db import DB_PATH
from core.factor_lab_engine import ALPHA158_FACTORS, MARKET_DEFAULTS, run_factor_lab
try:
    from api.factors import _active_gp_factors, _expr_vars_used, _gp_expr_to_math, _load_gp_alphas
except Exception:  # pragma: no cover - catalog can still serve base factors
    _active_gp_factors = None
    _expr_vars_used = None
    _gp_expr_to_math = None
    _load_gp_alphas = None

_CATALOG_BY_NAME = {str(f.get("name", "")).upper(): f for f in ALPHA158_FACTORS}


def _parse_account_factor_token(token: str) -> dict | None:
    """Convert a live account factor token like ROC_20 into a lab term."""
    s = str(token or "").strip().upper()
    if not s:
        return None
    if s in _CATALOG_BY_NAME:
        return {"mode": "factor", "factor": s, "periods": [], "weight": 1.0, "transform": "rank"}
    for base in sorted(_CATALOG_BY_NAME, key=len, reverse=True):
        prefix = base + "_"
        if s.startswith(prefix):
            tail = s[len(prefix):]
            if tail.isdigit():
                return {"mode": "factor", "factor": base, "periods": [int(tail)], "weight": 1.0, "transform": "rank"}
    return None


def _gp_account_item(account_id: str, group: str, strategy_name: str, factors_raw: str) -> dict | None:
    if _load_gp_alphas is None or _active_gp_factors is None or _gp_expr_to_math is None:
        return None
    backend = 'factor_miner_gp' if group == 'F' or factors_raw.startswith('FMGP') else 'gplearn'
    mined = _load_gp_alphas(account_id, backend=backend)
    active = _active_gp_factors(mined)
    factor_cards = []
    for alpha in active:
        expr = alpha.get('expression') or ''
        feature_cols = alpha.get('feature_cols')
        factor_cards.append({
            "name": alpha.get('name') or 'gp_factor',
            "latex": _gp_expr_to_math(expr, feature_cols),
            "s_expression": expr,
            "vars_used": (_expr_vars_used(expr, feature_cols) if _expr_vars_used is not None else []),
            "ic": alpha.get('ic'),
            "fitness": alpha.get('fitness'),
        })
    if not factor_cards:
        return None
    composite_latex = (
        r'\mathrm{score}(i)=\frac{1}{' + str(len(factor_cards)) + r'}\sum_{k=1}^{' + str(len(factor_cards)) + r'} f^{GP}_k(i)'
        if len(factor_cards) > 1 else
        r'\mathrm{score}(i)=f^{GP}(i)'
    )
    return {
        "account_id": account_id,
        "group": group,
        "strategy_name": strategy_name or account_id,
        "label": f'{account_id} · {strategy_name or ""}'.strip(),
        "factors": factors_raw,
        "runnable": False,
        "kind": "gp",
        "n_terms": len(factor_cards),
        "latex": composite_latex,
        "gp_factors": factor_cards,
    }


def _account_composite_rows(con: sqlite3.Connection, market: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT account_id, "group" AS group_name, strategy_name, factors
        FROM account_meta
        WHERE market = ?
          AND COALESCE(status, 'active') != 'retired'
        ORDER BY account_id
        """,
        (market,),
    ).fetchall()
    out: list[dict] = []
    seen: set[tuple] = set()
    for r in rows:
        raw = str(r["factors"] or "").strip()
        group = str(r["group_name"] or "")
        account_id = str(r["account_id"] or "")
        strategy_name = str(r["strategy_name"] or "")
        if not raw or group == "IDX" or raw.startswith("qlib_"):
            continue
        if raw.startswith(("GP(", "FMGP(")) or group in {"B", "F"}:
            item = _gp_account_item(account_id, group, strategy_name, raw)
            if item:
                out.append(item)
            continue
        terms = []
        for tok in raw.split(','):
            term = _parse_account_factor_token(tok)
            if term:
                terms.append(term)
        if not terms:
            continue
        key = tuple((t["factor"], tuple(t.get("periods") or [])) for t in terms)
        if key in seen:
            continue
        seen.add(key)
        w = round(1.0 / len(terms), 4)
        for t in terms:
            t["weight"] = w
        out.append({
            "account_id": account_id,
            "group": group,
            "strategy_name": strategy_name or account_id,
            "label": f'{account_id} · {strategy_name or ""}'.strip(),
            "factors": raw,
            "runnable": True,
            "kind": "factor_combo",
            "terms": terms,
            "n_terms": len(terms),
        })
    return out

router = APIRouter(prefix="/api/factor-lab", tags=["factor_lab"])


class FactorTermRequest(BaseModel):
    mode: Literal["factor", "latex"] = "factor"
    factor: str | None = None
    latex: str | None = Field(None, max_length=6000)
    weight: float = Field(1.0, ge=-100, le=100)
    transform: Literal["rank", "zscore"] = "rank"
    period: int | None = Field(None, ge=1, le=252)
    periods: list[int] | None = None


class FactorExpressionRequest(BaseModel):
    terms: list[FactorTermRequest]
    final_transform: Literal["rank"] = "rank"


class FactorLabRunRequest(BaseModel):
    market: Literal["US", "CN"] = "US"
    start_date: str
    end_date: str
    initial_capital: float | None = Field(None, gt=0)
    top_n: int | None = Field(None, ge=1, le=500)
    rebalance: Literal["daily", "weekly", "monthly"] | None = None
    rebalance_days: int | None = Field(None, ge=1, le=60)
    hold_band_mult: int | None = Field(None, ge=1, le=10)
    cooldown_days: int | None = Field(None, ge=0, le=60)
    min_hold_days: int | None = Field(None, ge=0, le=60)
    horizon: int = Field(5, ge=1, le=60)
    window: int = Field(20, ge=5, le=120)
    expression: FactorExpressionRequest


@router.get("/catalog")
async def factor_lab_catalog(market: str = Query("US")):
    market = _validate_market(market)
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only = ON")
        ticker_pred = "ticker GLOB ?" if market == 'CN' else "ticker NOT GLOB ?"
        rows = con.execute(
            f"""
            SELECT factor_name, COUNT(*) AS rows_count, MIN(date) AS min_date, MAX(date) AS max_date,
                   COUNT(DISTINCT ticker) AS tickers
            FROM factor_values
            WHERE factor_group = 'alpha158'
              AND {ticker_pred}
            GROUP BY factor_name
            """,
            ('[0-9][0-9][0-9][0-9][0-9][0-9].S[HZ]',),
        ).fetchall()
        coverage = {r["factor_name"]: dict(r) for r in rows}
        account_composites = _account_composite_rows(con, market)
    finally:
        con.close()

    return {
        "market": market,
        "factors": [
            {
                **f,
                "coverage": coverage.get(f["name"], {}),
            }
            for f in ALPHA158_FACTORS
        ],
        "account_composites": account_composites,
        "defaults": {
            **MARKET_DEFAULTS.get(market, MARKET_DEFAULTS["US"]),
            "horizon": 5,
            "window": 20,
            "sample_expression": {
                "terms": [
                    {"factor": "ROC", "periods": [5, 10, 20], "weight": 0.4, "transform": "rank"},
                    {"factor": "MA_RATIO", "period": 20, "weight": 0.3, "transform": "rank"},
                    {"factor": "RSI", "period": 14, "weight": -0.2, "transform": "rank"},
                    {"factor": "VSTD", "period": 20, "weight": 0.1, "transform": "rank"},
                ],
                "final_transform": "rank",
            },
        },
        "method_notes": [
            "Each term is transformed cross-sectionally per date before weighting.",
            "The composite score is ranked again per date before top-N selection.",
            "Look-ahead guard: signal[t] is evaluated against close[t+1+h] / close[t+1] - 1.",
        ],
    }


@router.post("/run")
async def factor_lab_run(req: FactorLabRunRequest):
    try:
        payload = req.dict()
        payload["market"] = _validate_market(str(payload.get("market") or "US"))
        return await asyncio.to_thread(run_factor_lab, payload)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
