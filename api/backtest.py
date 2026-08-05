from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel
from datetime import datetime, timedelta
from core.db import fetch_all, fetch_one
from core.backtest_engine import new_job, get_job, run_backtest_job, JOBS
import asyncio

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

VALID_MARKETS = {'US', 'CN'}


def _validate_market(market: str) -> str:
    m = (market or 'US').upper()
    if m not in VALID_MARKETS:
        raise HTTPException(status_code=400, detail=f"invalid market '{market}'")
    return m


class BacktestRequest(BaseModel):
    accounts: list[str]
    start_date: str
    end_date: str
    initial_capital: float = 10000
    universe_size: int = 100
    market: str = 'US'


@router.post("/run")
async def backtest_run(req: BacktestRequest):
    """Start a new async backtest job; returns job_id for polling."""
    if not req.accounts:
        raise HTTPException(400, "未选择任何账户")
    market = _validate_market(req.market)
    job_id = new_job()
    asyncio.create_task(run_backtest_job(
        job_id, req.accounts, req.start_date, req.end_date,
        req.initial_capital, req.universe_size, market,
    ))
    return {"job_id": job_id}


@router.get("/job/{job_id}")
async def backtest_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    # Return shallow copy without embedding huge result unless done
    out = {"status": job["status"], "progress": job["progress"],
           "message": job["message"], "error": job.get("error")}
    if job["status"] == "done":
        out["result"] = job["result"]
    return out


@router.get("/accounts")
async def backtest_accounts(market: str = Query('US')):
    market = _validate_market(market)
    rows = await fetch_all(
        "SELECT account_id as account, strategy_name, description, \"group\", factors, "
        "status, retired_at, retire_reason "
        "FROM account_meta m "
        "WHERE market = :m "
        "AND EXISTS ("
        "  SELECT 1 FROM account_state s WHERE s.account=m.account_id AND s.market=m.market "
        "  UNION SELECT 1 FROM accounts a WHERE a.name=m.account_id AND a.market=m.market "
        "  UNION SELECT 1 FROM trades t WHERE t.account=m.account_id AND t.market=m.market"
        ") "
        "ORDER BY account_id",
        {'m': market}
    )
    return {"accounts": rows, "market": market}


@router.get("/date-range")
async def backtest_date_range(market: str = Query('US')):
    """Return sensible default date range for the picker.

    The real backtest fetches yfinance history on demand, so we're not
    constrained by what's in the trades table. Default to the last 90 days
    ending today, but also expose the trades-table range for reference.
    """
    market = _validate_market(market)
    today = datetime.utcnow().date()
    default_end = today.isoformat()
    default_start = (today - timedelta(days=90)).isoformat()
    row = await fetch_one(
        "SELECT MIN(timestamp) as min_date, MAX(timestamp) as max_date "
        "FROM trades WHERE market = :m", {'m': market}
    )
    return {
        "min_date": default_start,
        "max_date": default_end,
        "trades_min": row["min_date"] if row else None,
        "trades_max": row["max_date"] if row else None,
        "market": market,
    }


@router.get("/qlib-status")
async def backtest_qlib_status(market: str = Query('US')):
    """Status of the Qlib daily-checkpoint registry.

    Surfaces, to the backtest UI banner:
      - Why Q-account backtest is currently disabled (3 leakage vectors)
      - Per-model checkpoint coverage (first/last day, count, MB on disk)
      - The TODO/DONE list so the user knows where the work is

    The frontend renders this above the account selector with a yellow
    "暂不支持" pill. As soon as `min_date` for ALL 10 models is older than
    the user's picked start date, we'll lift the block (next milestone).
    """
    market = _validate_market(market)
    import sys as _sys, os as _os
    qt_root = _os.path.expanduser("~/quant-trading")
    if qt_root not in _sys.path:
        _sys.path.insert(0, qt_root)
    try:
        from factors.qlib_checkpoint import coverage_summary
        cov = coverage_summary(market)
    except Exception as e:
        cov = {"error": str(e), "market": market, "models": {}, "total_bytes": 0}

    earliest_full = None
    if cov.get("models"):
        firsts = [m["first"] for m in cov["models"].values() if m.get("first")]
        if firsts and len(firsts) >= 10:
            earliest_full = max(firsts)  # latest of all "first dates"

    return {
        "market": market,
        "coverage": cov,
        "earliest_full_replay_date": earliest_full,
        "models_with_checkpoints": len(cov.get("models", {})),
        "models_total": 10,
        "leakage_vectors": [
            {
                "id": "weights",
                "title": "模型权重前瞻 (致命)",
                "title_en": "Model-weight look-ahead (fatal)",
                "desc": "当前 cron 训练权重已见过最新数据；回测时直接读 factor_values 等于让模型作弊",
                "desc_en": "Current cron-trained weights have already seen the latest data; reading factor_values during backtest lets the model cheat.",
            },
            {
                "id": "normalization",
                "title": "特征归一化前瞻",
                "title_en": "Feature-normalization look-ahead",
                "desc": "RobustZScoreNorm 用全历史 fit μ/σ，回放时输入分布偷看了未来",
                "desc_en": "RobustZScoreNorm fits μ/σ on the full history, so the replay input distribution peeks into the future.",
            },
            {
                "id": "label",
                "title": "标签前瞻",
                "title_en": "Label look-ahead",
                "desc": "Qlib 默认 label=次日收益，训练集尾部需 ≥ T-2 才能算出 label",
                "desc_en": "Qlib's default label = next-day return, so the tail of the training set requires ≥ T-2 before the label can be computed.",
            },
            {
                "id": "survivorship",
                "title": "选股池幸存者偏差 (隐性)",
                "title_en": "Universe survivorship bias (latent)",
                "desc": "Russell 1000 = 今日存活者；ML 模型对此尤其敏感（同样影响 A/B 组）",
                "desc_en": "Russell 1000 = today's survivors; ML models are especially sensitive to this (A/B groups affected too).",
            },
        ],
        "plan": {
            "approach": "Step 1: 每日 cron 写 frozen checkpoint (从 2026-04-30 起累积)；Step 2: 回测引擎按日 load_checkpoint → predict，零穿越零再训练",
            "approach_en": "Step 1: daily cron writes a frozen checkpoint (accumulating from 2026-04-30); Step 2: backtest engine load_checkpoint → predict per day, zero leakage, zero retraining.",
            "checkpoint_size_per_day_kb": 1200,
            "yearly_storage_mb": 300,
        },
        "todo": [
            {"id": "ckpt-module", "text": "factors/qlib_checkpoint.py: save/load/coverage", "text_en": "factors/qlib_checkpoint.py: save/load/coverage", "done": True},
            {"id": "ckpt-cron-wire", "text": "qlib_signal.run_one_model 每次训练后写 checkpoint", "text_en": "qlib_signal.run_one_model writes a checkpoint after each training run", "done": True},
            {"id": "ckpt-block-q", "text": "回测引擎拒绝 Q 账户并返回明确报错", "text_en": "Backtest engine rejects Q accounts with an explicit error", "done": True},
            {"id": "ckpt-ui-banner", "text": "回测页顶部状态条 (问题/计划/TODO)", "text_en": "Status banner on the backtest page (issues / plan / TODO)", "done": True},
            {"id": "ckpt-ui-disable", "text": "Q 账户 checkbox 灰掉 + tooltip 说明", "text_en": "Q-account checkboxes greyed out with tooltip explanation", "done": True},
            {"id": "ckpt-self-test", "text": "load_checkpoint 自检 (env 漂移检测)", "text_en": "load_checkpoint self-test (env-drift detection)", "done": True},
            {"id": "ckpt-accumulate", "text": "积累 ≥20 个交易日 checkpoint (从 2026-04-30 起)", "text_en": "Accumulate ≥20 trading days of checkpoints (from 2026-04-30)", "done": False},
            {"id": "ckpt-replay-engine", "text": "backtest_engine: Q 账户走 load_checkpoint 路径", "text_en": "backtest_engine: Q accounts use the load_checkpoint path", "done": False},
            {"id": "ckpt-cn-mirror", "text": "CN qlib bin export 验证 + CQ 账户 checkpoint 共享", "text_en": "Validate CN qlib bin export + share checkpoints with CQ accounts", "done": False},
            {"id": "ckpt-replay-tests", "text": "replay 一致性测试: cron 写 score == 隔日 load 重 predict score", "text_en": "Replay consistency test: cron-written score == score from next-day load + repredict", "done": False},
            {"id": "ckpt-survivorship", "text": "(可选) 选股池历史快照解决幸存者偏差", "text_en": "(Optional) Historical universe snapshots to address survivorship bias", "done": False},
        ],
        "done": [
            "frozen checkpoint 模块上线 (~/quant-trading/factors/qlib_checkpoint.py)",
            "每日 23:00 UTC cron 训练后自动写 .pkl + .json 元数据",
            "checkpoint 包含模型 + 已 fit 的 handler/processors (避免归一化穿越)",
            "self-test fingerprint: 记录首行特征哈希 + 期望 score, load 时验证",
            "回测页面拒绝 Q 账户并展示阻塞原因",
        ],
        "done_en": [
            "Frozen-checkpoint module shipped (~/quant-trading/factors/qlib_checkpoint.py)",
            "Daily 23:00 UTC cron training auto-writes .pkl + .json metadata",
            "Checkpoint includes model + fitted handler/processors (prevents normalization leakage)",
            "Self-test fingerprint: records first-row feature hash + expected score, verified on load",
            "Backtest page rejects Q accounts and shows the blocking reason",
        ],
    }
