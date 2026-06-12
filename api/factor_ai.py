"""
AI factor interpretation endpoint.

Calls Hermes Agent as a subprocess to generate a markdown analysis of a strategy's
factors + current holdings + recent trades. Results are cached per (account_id, lang)
for 2 hours.
"""
from __future__ import annotations
import asyncio
import json
import os
import time
import shutil
from typing import Any
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix='/api/factor_ai', tags=['factor_ai'])

CACHE_TTL = 2 * 3600  # 2 hours
HERMES_BIN = shutil.which('hermes') or os.path.expanduser('~/.local/bin/hermes')
SUBPROC_TIMEOUT = 180  # seconds — LLM call cap

# In-process cache: { (account_id, lang): {markdown, created_at} }
_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
# Per-key inflight lock so concurrent expands don't fire two subprocesses
_INFLIGHT: dict[tuple[str, str], asyncio.Task] = {}


def _build_prompt(payload: dict, lang: str) -> str:
    account_id = payload.get('account_id', '?')
    group = payload.get('group', '?')
    strategy_name = payload.get('strategy_name', '')
    factors = payload.get('factors') or []
    composite = payload.get('composite') or {}
    positions = payload.get('positions') or []
    trades = payload.get('trades') or []
    gp_info = payload.get('gp_info') or ''
    signal_quality = payload.get('signal_quality') or {}

    # Trim payload — LLMs choke on huge JSON
    factors_min = []
    for f in factors[:20]:
        item = {k: f.get(k) for k in (
            'name', 'formula', 'latex', 's_expression',
            'physics', 'intuition', 'motivation', 'alpha_source',
            'ic', 'fitness',
        ) if f.get(k)}
        factors_min.append(item)
    positions_min = [
        {
            'ticker': p.get('ticker') or p.get('symbol'),
            'shares': p.get('shares') or p.get('qty'),
            'avg_cost': p.get('avg_cost') or p.get('cost'),
            'current_price': p.get('current_price') or p.get('price'),
        }
        for p in positions[:30]
    ]
    trades_min = [
        {
            'time': (t.get('timestamp') or t.get('time') or '')[:19],
            'ticker': t.get('ticker') or t.get('symbol'),
            'side': t.get('side'),
            'shares': t.get('shares') or t.get('qty'),
            'price': t.get('price'),
        }
        for t in (trades[-30:] if isinstance(trades, list) else [])
    ]

    data_json = json.dumps({
        'account_id': account_id,
        'group': group,
        'strategy_name': strategy_name,
        'gp_info': gp_info,
        'factors': factors_min,
        'composite': {
            'latex': composite.get('latex'),
            'aggregation': composite.get('aggregation'),
            'weights': composite.get('weights'),
            'n_factors': composite.get('n_factors'),
        },
        'signal_quality': signal_quality,
        'positions': positions_min,
        'recent_trades': trades_min,
    }, ensure_ascii=False, indent=2)

    # --- Pipeline disclosure ------------------------------------------------
    # Tell the LLM EXACTLY how factors are combined so it doesn't assume the
    # "industry standard" z-score → mean pipeline. Our A-group uses
    # rank → mean → rank (see SignalGenerator in ~/quant-trading/factors/signal.py).
    # GP and Qlib have their own aggregations — only emit the A-group note when
    # mean_then_rank actually applies.
    agg = (composite.get('aggregation') or '').strip()
    pipeline_note_zh = ''
    pipeline_note_en = ''
    if agg == 'mean_then_rank':
        pipeline_note_zh = (
            "\n**重要：合成管线（请勿假设是 z-score）**\n"
            "本账户用的是 **rank → mean → rank**，不是 z-score：\n"
            "1. 每个因子在当日全市场做 `rank(pct=True)`，落到 [0,1]（这一步同时解决量纲差异和肥尾敏感性——比 z-score 更稳健）。\n"
            "2. 等权 `mean` 跨因子得到 composite。\n"
            "3. 再做一次截面 rank 出最终 signal，取 top_n。\n"
            "评价时请基于这个真实管线判断，不要套用 z-score 的肥尾/异常值论述。\n"
        )
        pipeline_note_en = (
            "\n**Important: composition pipeline (do NOT assume z-score)**\n"
            "This account uses **rank → mean → rank**, not z-score:\n"
            "1. Each factor is cross-sectionally rank-transformed (`rank(pct=True)`, range [0,1]) on each bar. This simultaneously neutralizes unit differences and is robust to fat tails — strictly more conservative than z-score.\n"
            "2. Equal-weight `mean` across factors → composite.\n"
            "3. Final cross-sectional rank → signal, take top_n.\n"
            "Critique the strategy based on this actual pipeline. Do NOT use z-score-specific arguments (fat-tail blow-up, outlier dominance) — they don't apply.\n"
        )

    if lang == 'en':
        prompt = f"""You are a senior quantitative trader and factor researcher. Below is a real production account from a US-equity systematic trading system. Write a professional, opinionated analysis in **English Markdown**.

Account data (JSON):
```json
{data_json}
```
{pipeline_note_en}
Produce these sections, in order, with `##` headers:

## 1. Factor Composition
Break down each factor: what raw inputs it uses, how the formula combines them, what time scale it operates on. Be concrete — name the variables.

## 2. Mathematical / Physical Meaning
For each factor, explain the math intuition (derivatives, moments, ratios, etc.) and the underlying market microstructure or behavioral hypothesis. Don't just rephrase the formula — interpret it.

## 3. Signal Quality Evidence (Rank IC / ICIR)
Use the `signal_quality` block if present. Interpret mean Rank IC, ICIR, rolling ICIR, win rate, sample size, universe size, and warnings. Be statistically honest: short samples are directional evidence, not proof. Distinguish “signal predicts cross-section” from “portfolio made money”.

## 4. LLM Critique
Your honest assessment: strengths, weaknesses, redundancies between factors, look-ahead / overfitting risks, regime sensitivity. Be specific. If two factors are essentially the same thing, say so. If something looks fragile, flag it. The Signal Quality section should inform this critique — do not ignore negative IC/ICIR.

## 5. Trading-Floor Perspective (Holdings + Recent Trades)
Look at the current positions and recent trades. Comment as a working trader:
- Does the portfolio composition match what the factors should produce?
- Concentration / sector tilt / obvious risks?
- Anything in the recent trade flow that looks wrong or inconsistent with the stated strategy?
- One actionable observation a portfolio manager would care about.

Rules:
- Markdown only. No code fences around the whole response.
- Be terse and high-signal. No filler, no "in summary".
- Use bullet points where it helps; prose where it helps.
- If data is missing for a section, say so briefly and move on.
"""
    else:
        prompt = f"""你是一位资深量化交易员和因子研究员。下面是某美股量化交易系统中一个真实生产账户的快照。请用**中文 Markdown** 写一份专业的、有观点的解读。

账户数据（JSON）：
```json
{data_json}
```
{pipeline_note_zh}
按以下顺序输出，每段用 `##` 标题：

## 1. 因子组成
逐个拆解每个因子：用了哪些原始输入、公式怎么组合、作用在什么时间尺度上。具体一点——把变量点名。

## 2. 数学 / 物理含义
逐个解释每个因子的数学直觉（导数、矩、比值等）以及背后的市场微结构或行为金融假设。不要只复述公式——要解读。

## 3. 信号质量证据（Rank IC / ICIR）
如果 `signal_quality` 存在，请使用它判断。解读平均 Rank IC、ICIR、滚动 ICIR、IC 胜率、样本天数、股票池规模和 warnings。统计上要诚实：短样本只能作为方向证据，不是证明。区分“信号能预测横截面”和“组合最后赚了钱”。

## 4. LLM 评价
你的真实判断：因子的优点、缺点、彼此之间的冗余、look-ahead / 过拟合风险、对市场状态的敏感度。要具体。如果两个因子本质上一样，直接说出来；如果哪里看起来脆弱，指出来。信号质量数据必须进入你的判断——不要无视负 IC / 负 ICIR。

## 5. 交易员视角（持仓 + 最近交易）
看当前持仓和最近交易，以一线交易员的视角点评：
- 组合构成和因子应该产出的结果是否一致？
- 集中度 / 行业偏向 / 明显风险？
- 最近的交易流水里有没有看起来奇怪、与策略声称不符的地方？
- 一条投资组合经理会在意的可操作的观察。

要求：
- 仅输出 Markdown，整段不要包在代码块里。
- 简练、信息密度高。不要废话，不要"综上所述"。
- 该用列表用列表，该用散文用散文。
- 如果某节缺数据就简短说明并跳过。
"""
    return prompt


async def _run_hermes(prompt: str) -> str:
    """Call `hermes chat -q PROMPT -Q -t ''` and return stdout (stripped of the leading session_id line)."""
    if not HERMES_BIN or not os.path.exists(HERMES_BIN):
        raise RuntimeError(f'hermes binary not found at {HERMES_BIN}')
    proc = await asyncio.create_subprocess_exec(
        HERMES_BIN, 'chat', '-q', prompt, '-Q', '-t', '',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SUBPROC_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f'hermes call timed out after {SUBPROC_TIMEOUT}s')
    if proc.returncode != 0:
        raise RuntimeError(f'hermes exited {proc.returncode}: {stderr.decode("utf-8", "replace")[:500]}')
    text = stdout.decode('utf-8', 'replace')
    # Strip the leading "session_id: ..." line that -Q still emits
    lines = text.splitlines()
    out_lines = [ln for ln in lines if not ln.startswith('session_id:')]
    return '\n'.join(out_lines).strip()


async def _generate(key: tuple[str, str], payload: dict, lang: str) -> dict:
    prompt = _build_prompt(payload, lang)
    md = await _run_hermes(prompt)
    entry = {'markdown': md, 'created_at': time.time()}
    _CACHE[key] = entry
    return entry


@router.post('/{account_id}')
async def get_factor_ai(account_id: str, payload: dict, lang: str = Query('zh')):
    if lang not in ('zh', 'en'):
        lang = 'zh'
    key = (account_id, lang)
    now = time.time()

    # Serve from cache if fresh
    entry = _CACHE.get(key)
    if entry and now - entry['created_at'] < CACHE_TTL:
        return {
            'markdown': entry['markdown'],
            'cached': True,
            'created_at': entry['created_at'],
            'ttl_remaining': int(CACHE_TTL - (now - entry['created_at'])),
        }

    # Coalesce concurrent calls
    task = _INFLIGHT.get(key)
    if task is None or task.done():
        task = asyncio.create_task(_generate(key, payload, lang))
        _INFLIGHT[key] = task

    try:
        entry = await task
    except Exception as e:
        _INFLIGHT.pop(key, None)
        raise HTTPException(status_code=502, detail=f'AI generation failed: {e}')
    _INFLIGHT.pop(key, None)
    return {
        'markdown': entry['markdown'],
        'cached': False,
        'created_at': entry['created_at'],
        'ttl_remaining': CACHE_TTL,
    }


@router.delete('/{account_id}')
async def invalidate(account_id: str, lang: str = Query('zh')):
    """Manual cache bust (debug / 'regenerate' button hook)."""
    _CACHE.pop((account_id, lang), None)
    return {'ok': True}
