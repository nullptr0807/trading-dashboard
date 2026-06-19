from __future__ import annotations

import asyncio
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.backtest import _validate_market
from core.db import DB_PATH
from core.factor_lab_engine import ALPHA158_FACTORS, run_factor_lab

router = APIRouter(prefix="/api/factor-lab", tags=["factor_lab"])


class FactorTermRequest(BaseModel):
    factor: str
    weight: float = Field(1.0, ge=-100, le=100)
    transform: Literal["rank", "zscore"] = "rank"


class FactorExpressionRequest(BaseModel):
    terms: list[FactorTermRequest]
    final_transform: Literal["rank"] = "rank"


class FactorLabRunRequest(BaseModel):
    market: Literal["US", "CN"] = "US"
    start_date: str
    end_date: str
    initial_capital: float = Field(10000, gt=0)
    universe_size: int = Field(300, ge=10, le=1200)
    top_n: int = Field(20, ge=1, le=500)
    cost_bps: float = Field(5, ge=0, le=100)
    rebalance: Literal["daily", "weekly", "monthly"] = "weekly"
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
        "defaults": {
            "initial_capital": 100000 if market == "CN" else 10000,
            "universe_size": 300,
            "top_n": 20,
            "cost_bps": 5,
            "rebalance": "weekly",
            "horizon": 5,
            "window": 20,
            "sample_expression": {
                "terms": [
                    {"factor": "ROC_20", "weight": 0.4, "transform": "rank"},
                    {"factor": "MA_RATIO_20", "weight": 0.3, "transform": "rank"},
                    {"factor": "RSI_14", "weight": -0.2, "transform": "rank"},
                    {"factor": "VSTD_20", "weight": 0.1, "transform": "rank"},
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
