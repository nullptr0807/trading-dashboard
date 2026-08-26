from __future__ import annotations

import asyncio
import re
import sqlite3
import time
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
_LEGACY_GP_FEATURE_COLS = [
    'o_c', 'h_c', 'l_c', 'v_vma20', 'ma_5', 'ma_10', 'ma_20',
    'std_5', 'std_10', 'std_20', 'ret_1', 'ret_5', 'ret_10',
]


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


def _feature_to_formula_symbol(name: str) -> str | None:
    n = str(name or '').strip()
    mapping = {
        'ret_1': 'ROC_1', 'ret_5': 'ROC_5', 'ret_10': 'ROC_10',
        # GP defines ma_N = rolling_mean(close,N) / close, while Factor Lab's
        # MA_RATIO_N is close / rolling_mean(close,N). Preserve the GP
        # coordinate exactly instead of silently inverting the signal.
        'ma_5': '(1/MA_RATIO_5)', 'ma_10': '(1/MA_RATIO_10)', 'ma_20': '(1/MA_RATIO_20)',
        'std_5': 'STD_5', 'std_10': 'STD_10', 'std_20': 'STD_20',
        'v_vma20': 'VMOM_20',
        'o_c': '(OPEN/CLOSE)', 'h_c': '(HIGH/CLOSE)', 'l_c': '(LOW/CLOSE)',
        'range_pos': '((CLOSE-LOW)/(HIGH-LOW))',
        'upper_pos': '((HIGH-CLOSE)/(HIGH-LOW))',
        'lower_shadow': None,
        'upper_shadow': None,
        'gap_1': '(OPEN/lag(CLOSE,1)-1)',
        'dvol_vma20': '((CLOSE*VOLUME)/mean(CLOSE*VOLUME,20))',
        'ret_1_dvol': '(ROC_1/((CLOSE*VOLUME)/1000000000))',
        'absret_1_dvol': '(abs(ROC_1)/((CLOSE*VOLUME)/1000000000))',
        'vol_of_vol_20': 'std(VMOM_20,20)',
        'skew_20': None,
        'kurt_20': None,
        'pv_corr_20': 'rho_20(ROC_1,delta(VOLUME,1))',
        'slope_20': 'BETA_20',
        'trend_r2_20': None,
        'trend_resi_20': None,
    }
    return mapping.get(n)


def _gp_expr_to_runnable_formula(expr: str, feature_cols: list[str] | None = None) -> str | None:
    """Translate simple gplearn S-expressions into Factor Lab formula syntax."""
    cols = feature_cols or []
    tokens = re.findall(r'[A-Za-z_][A-Za-z0-9_]*|-?\d+\.?\d*|[(),]', str(expr or ''))
    pos = 0

    def parse():
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError('unexpected end')
        tok = tokens[pos]
        pos += 1
        if re.fullmatch(r'-?\d+\.?\d*', tok):
            return tok
        if tok.startswith('X') and tok[1:].isdigit():
            idx = int(tok[1:])
            if idx >= len(cols):
                raise ValueError('feature index out of range')
            mapped = _feature_to_formula_symbol(cols[idx])
            if not mapped:
                raise ValueError(f'unsupported feature {cols[idx]}')
            return mapped
        if pos < len(tokens) and tokens[pos] == '(':
            pos += 1
            args = []
            if pos < len(tokens) and tokens[pos] != ')':
                args.append(parse())
                while pos < len(tokens) and tokens[pos] == ',':
                    pos += 1
                    args.append(parse())
            if pos >= len(tokens) or tokens[pos] != ')':
                raise ValueError('missing )')
            pos += 1
            if tok == 'add' and len(args) == 2:
                return f'({args[0]}+{args[1]})'
            if tok == 'sub' and len(args) == 2:
                return f'({args[0]}-{args[1]})'
            if tok == 'mul' and len(args) == 2:
                return f'({args[0]}*{args[1]})'
            if tok == 'div' and len(args) == 2:
                return f'({args[0]}/({args[1]}))'
            if tok == 'sqrt_abs' and len(args) == 1:
                return f'sqrt(abs({args[0]}))'
            if tok == 'log_abs1' and len(args) == 1:
                return f'log(abs({args[0]})+1)'
            if tok == 'neg' and len(args) == 1:
                return f'-({args[0]})'
            if tok == 'inv' and len(args) == 1:
                return f'(1/({args[0]}))'
            if tok == 'max2' and len(args) == 2:
                return f'max2({args[0]},{args[1]})'
            if tok == 'min2' and len(args) == 2:
                return f'min2({args[0]},{args[1]})'
            raise ValueError(f'unsupported GP function {tok}')
        mapped = _feature_to_formula_symbol(tok)
        if mapped:
            return mapped
        raise ValueError(f'unsupported token {tok}')

    try:
        out = parse()
        if pos != len(tokens):
            return None
        return out
    except Exception:
        return None


def _gp_account_item(account_id: str, group: str, strategy_name: str, factors_raw: str) -> dict | None:
    if _load_gp_alphas is None or _active_gp_factors is None or _gp_expr_to_math is None:
        return None
    backend = 'factor_miner_gp' if group == 'F' or factors_raw.startswith('FMGP') else 'gplearn'
    mined = _load_gp_alphas(account_id, backend=backend)
    active = _active_gp_factors(mined)
    factor_cards = []
    runnable_terms = []
    for alpha in active:
        expr = alpha.get('expression') or ''
        # B01-B10 legacy artifacts predate persisted feature_cols. Their X0..
        # mapping is frozen by gp_miner.FEATURE_COLS and must not be guessed as
        # an empty list (which made valid legacy accounts unrunnable).
        feature_cols = alpha.get('feature_cols') or _LEGACY_GP_FEATURE_COLS
        runnable_formula = _gp_expr_to_runnable_formula(expr, feature_cols)
        factor_cards.append({
            "name": alpha.get('name') or 'gp_factor',
            "latex": _gp_expr_to_math(expr, feature_cols),
            "s_expression": expr,
            "runnable_formula": runnable_formula,
            "vars_used": (_expr_vars_used(expr, feature_cols) if _expr_vars_used is not None else []),
            "ic": alpha.get('ic'),
            "fitness": alpha.get('fitness'),
        })
        if runnable_formula:
            runnable_terms.append({"mode": "latex", "latex": runnable_formula, "weight": 1.0, "transform": "rank"})
    if not factor_cards:
        return None
    can_run = len(runnable_terms) == len(factor_cards)
    if can_run and runnable_terms:
        w = round(1.0 / len(runnable_terms), 4)
        for t in runnable_terms:
            t["weight"] = w
    composite_latex = (
        r'\mathrm{score}(i)=\frac{1}{' + str(len(factor_cards)) + r'}\sum_{k=1}^{' + str(len(factor_cards)) + r'} f^{GP}_k(i)'
        if len(factor_cards) > 1 else
        r'\mathrm{score}(i)=f^{GP}(i)'
    )
    composite_text = (
        'score(i) = rank(' + ' + '.join([f'{t["weight"]:.4g} * {t["latex"]}' for t in runnable_terms]) + ')'
        if can_run and runnable_terms else
        f'score(i) = mean({len(factor_cards)} GP factor expression(s))'
    )
    return {
        "account_id": account_id,
        "group": group,
        "strategy_name": strategy_name or account_id,
        "label": f'{account_id} · {strategy_name or ""}'.strip(),
        "factors": factors_raw,
        "runnable": can_run,
        "kind": "gp",
        "n_terms": len(factor_cards),
        "latex": composite_latex,
        "latex_text": composite_text,
        "terms": runnable_terms if can_run else [],
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
    dataset_scope: Literal["configured", "factor_coverage", "priced"] = "configured"
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


_CATALOG_CACHE_TTL = 60.0
_CATALOG_CACHE: dict[str, tuple[float, dict]] = {}


def _factor_lab_catalog_sync(market: str) -> dict:
    """Run the catalog's aggregate scans off the asyncio event loop."""
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
        "factors": [{**f, "coverage": coverage.get(f["name"], {})} for f in ALPHA158_FACTORS],
        "account_composites": account_composites,
        "defaults": {
            **MARKET_DEFAULTS.get(market, MARKET_DEFAULTS["US"]),
            "dataset_scope": "configured",
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


@router.get("/catalog")
async def factor_lab_catalog(market: str = Query("US")):
    market = _validate_market(market)
    cached = _CATALOG_CACHE.get(market)
    now = time.monotonic()
    if cached and now - cached[0] < _CATALOG_CACHE_TTL:
        return cached[1]
    payload = await asyncio.to_thread(_factor_lab_catalog_sync, market)
    _CATALOG_CACHE[market] = (time.monotonic(), payload)
    return payload


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
