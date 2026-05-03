from fastapi import APIRouter, Query, HTTPException
import json
import os
import re
from core.db import fetch_one

VALID_MARKETS = {'US', 'CN'}


def _validate_market(market: str) -> str:
    m = (market or 'US').upper()
    if m not in VALID_MARKETS:
        raise HTTPException(status_code=400, detail=f"invalid market '{market}'")
    return m
from core.factor_formulas import (
    FACTOR_FORMULAS, FACTOR_LATEX, FACTOR_EXPLANATIONS,
    FEATURE_COLS, GP_VAR_MATH,
    gp_expr_to_math,
)
from core.factor_formulas_en import (
    FACTOR_EXPLANATIONS_EN, GP_VAR_DESC_EN,
)
from core.gp_explain import explain as gp_explain

# Pull bilingual Q-strategy descriptions from quant-trading (single source of truth).
import sys as _sys
_QT_ROOT = os.path.expanduser('~/quant-trading')
if _QT_ROOT not in _sys.path:
    _sys.path.insert(0, _QT_ROOT)
try:
    from accounts.qlib_strategies import QLIB_STRATEGIES as _QLIB_STRATEGIES
    _QLIB_BY_BASEID = {s.id: s for s in _QLIB_STRATEGIES}
except Exception:
    _QLIB_BY_BASEID = {}

router = APIRouter(prefix='/api/factors', tags=['factors'])

MINED_ALPHAS_PATH = os.path.expanduser(
    '~/quant-trading/factors/mined_alphas_per_account.json'
)

GP_VAR_DESC_ZH = {
    'o_c':     '开盘/收盘比',
    'h_c':     '最高/收盘比',
    'l_c':     '最低/收盘比',
    'v_vma20': '量能相对20日均量',
    'ma_5':    '5日均线/收盘',
    'ma_10':   '10日均线/收盘',
    'ma_20':   '20日均线/收盘',
    'std_5':   '5日波动率/价格',
    'std_10':  '10日波动率/价格',
    'std_20':  '20日波动率/价格',
    'ret_1':   '昨日收益率',
    'ret_5':   '5日收益率',
    'ret_10':  '10日收益率',
}


def _load_gp_alphas(account_id: str) -> list[dict]:
    try:
        with open(MINED_ALPHAS_PATH) as f:
            data = json.load(f)
        return data.get(account_id, []) or []
    except Exception:
        return []


def _expr_vars_used(expr: str) -> list[str]:
    seen = []
    for m in re.finditer(r'\bX(\d+)\b', expr):
        idx = int(m.group(1))
        if idx < len(FEATURE_COLS):
            v = FEATURE_COLS[idx]
            if v not in seen:
                seen.append(v)
    return seen


_GP_PARAM_TEXT = {
    'zh': {
        'seed_name': 'seed（随机种子）',
        'seed_detail': '遗传算法初始族群是随机生成的。固定 seed 是为了可复现——同一个 seed 会得到同一套进化轨迹。不同 B 账户用不同 seed，相当于从不同初始条件做多次平行宇宙实验，再挑表现最好的表达式。',
        'pop_name': 'pop（种群规模）',
        'pop_detail': '每一代同时存活 {pop} 个候选因子表达式。类似达尔文进化中的"物种基数"——pop 越大搜索空间覆盖越广，不容易陷入局部最优，但也越慢。300 属轻量探索，600+ 属精细搜索。',
        'gen_name': 'gen（进化代数）',
        'gen_detail': '种群演化 {gen} 代后停止。每代做：选择（保留适应度高的）→ 交叉（父代互换子树）→ 变异（随机替换子节点）。代数越多越充分优化，但也越容易过拟合。20–30 是常见折中。',
    },
    'en': {
        'seed_name': 'seed (random seed)',
        'seed_detail': 'The GA initial population is generated randomly. A fixed seed ensures reproducibility — the same seed reproduces the same evolutionary trajectory. Different B-accounts use different seeds, akin to running parallel-universe experiments from different initial conditions and picking the best expression.',
        'pop_name': 'pop (population size)',
        'pop_detail': 'Each generation keeps {pop} candidate factor expressions alive simultaneously. Analogous to the "species base count" in Darwinian evolution — larger pop covers more of the search space and avoids local optima, but is slower. 300 is lightweight exploration; 600+ is fine-grained search.',
        'gen_name': 'gen (number of generations)',
        'gen_detail': 'The population evolves for {gen} generations before stopping. Each generation: selection (keep the fittest) → crossover (swap subtrees between parents) → mutation (randomly replace subnodes). More generations → more optimization, but also more overfitting. 20–30 is a common trade-off.',
    },
}


def _gp_param_breakdown(gp_info: str, lang: str = 'zh') -> list[dict]:
    T = _GP_PARAM_TEXT.get(lang, _GP_PARAM_TEXT['zh'])
    m = re.search(r'seed\s*=\s*(\d+)', gp_info)
    seed = int(m.group(1)) if m else None
    m = re.search(r'pop\s*=\s*(\d+)', gp_info)
    pop = int(m.group(1)) if m else None
    m = re.search(r'gen\s*=\s*(\d+)', gp_info)
    gen = int(m.group(1)) if m else None
    items = []
    if seed is not None:
        items.append({'name': T['seed_name'], 'value': str(seed), 'detail': T['seed_detail']})
    if pop is not None:
        items.append({'name': T['pop_name'], 'value': str(pop), 'detail': T['pop_detail'].format(pop=pop)})
    if gen is not None:
        items.append({'name': T['gen_name'], 'value': str(gen), 'detail': T['gen_detail'].format(gen=gen)})
    return items


_A_MOTIVATION = {
    'zh': {
        'reversion_label': '回归',
        'body': (
            '该账户用 {N} 个因子。合成方式：等权平均 → 横截面百分位排名 → 取 Top/Bottom。\n\n'
            '每个因子权重都是 1/{N} ≈ {w:.3f}。\n\n'
            '为什么等权？我们没有先验知道哪个因子更准，人为调权容易过拟合历史。'
            '后续的 rank 步骤会抹掉量纲差异，让因子公平投票（类似 Borda count 选举）。\n\n'
            '⚠️ 已知简化：先对原始因子值直接求均值再 rank。量纲不同的因子里，绝对值大的会主导 score。'
            '更规范做法是"先对每个因子横截面 rank，再平均 rank"。'
        ),
        'reversion_note': '\n\n此账户是均值回归类，会额外做 1 - rank 反转：分数越低越看多。',
    },
    'en': {
        'reversion_label': 'reversion',
        'body': (
            'This account uses {N} factors. Aggregation: equal-weight mean → cross-sectional percentile rank → take Top/Bottom.\n\n'
            'Each factor gets a weight of 1/{N} ≈ {w:.3f}.\n\n'
            'Why equal weights? We have no prior on which factor is most accurate; hand-tuning weights easily overfits history. '
            'The subsequent rank step removes scale differences, letting factors vote fairly (similar to a Borda count).\n\n'
            '⚠️ Known simplification: we mean raw factor values first, then rank. When factors have different scales, the largest-magnitude factor dominates the score. '
            'A cleaner approach is to "cross-sectionally rank each factor first, then average the ranks".'
        ),
        'reversion_note': '\n\nThis account is mean-reversion type, so we additionally apply 1 − rank: lower scores become more bullish.',
    },
}


_B_MOTIVATION = {
    'zh': {
        'multi': (
            '本账户的 {n} 个 GP 因子通过"等权平均 → 横截面 rank → 取 Top N"合成最终信号。'
            '等权是默认选择——因为没有先验偏好，rank 步骤又会抹平量纲差异。\n\n'
            '每个因子的具体动机、数学含义、alpha 来源见上方各张卡片。'
        ),
        'single': (
            '本账户只用 1 个 GP 表达式直接作为打分函数：对每只股票代入 → 实数 → 横截面百分位 rank → 取 Top N。\n\n'
            '不需要加权，因为 GP 已经把"怎么组合原始特征"这件事内化进表达式树了。具体动机见上方卡片。'
        ),
        'none': (
            '本账户暂未找到挖掘记录（mined_alphas_per_account.json 中无此账户条目）。\n\n'
            '可能原因：GP 训练尚未跑完，或结果未被持久化。'
        ),
        'note': 'GP 表达式来自 factors/mined_alphas_per_account.json，训练时持久化。',
    },
    'en': {
        'multi': (
            'The {n} GP factors in this account are combined into the final signal via "equal-weight mean → cross-sectional rank → take Top N". '
            'Equal weighting is the default — no prior preference, and the rank step smooths out scale differences.\n\n'
            'See the cards above for each factor\'s motivation, mathematical meaning, and alpha source.'
        ),
        'single': (
            'This account uses a single GP expression directly as the scoring function: plug in each stock → real number → cross-sectional percentile rank → take Top N.\n\n'
            'No weighting is needed — GP has already internalized "how to combine raw features" inside the expression tree. See the card above for motivation.'
        ),
        'none': (
            'No mined records were found for this account (no entry in mined_alphas_per_account.json).\n\n'
            'Possible causes: GP training has not finished, or results were not persisted.'
        ),
        'note': 'GP expressions come from factors/mined_alphas_per_account.json, persisted during training.',
    },
}


@router.get('/{account_id}')
async def get_factors(account_id: str, lang: str = 'zh', market: str = Query('US')):
    market = _validate_market(market)
    if lang not in ('en', 'zh'):
        lang = 'zh'
    meta = await fetch_one(
        'SELECT * FROM account_meta WHERE account_id = :a AND market = :m',
        {'a': account_id, 'm': market}
    )
    if not meta:
        return {'error': 'Account not found'}

    group = meta.get('group', '')
    factors_str = meta.get('factors', '')
    strategy_name = meta.get('strategy_name', '')

    EXPL_TABLE = FACTOR_EXPLANATIONS_EN if lang == 'en' else FACTOR_EXPLANATIONS

    if group == 'A':
        factor_names = [f.strip() for f in factors_str.split(',') if f.strip()]
        factors = []
        for name in factor_names:
            exp = EXPL_TABLE.get(name) or FACTOR_EXPLANATIONS.get(name, {})
            if isinstance(exp, str):
                physics, motivation = exp, ''
            else:
                physics = exp.get('physics', '')
                motivation = exp.get('motivation', '')
            factors.append({
                'name': name,
                'formula': FACTOR_FORMULAS.get(name, ''),
                'latex': FACTOR_LATEX.get(name, ''),
                'physics': physics,
                'motivation': motivation,
            })

        A = _A_MOTIVATION[lang]
        is_mean_rev = ('回归' in strategy_name) or ('reversion' in strategy_name.lower())
        N = len(factor_names)
        inside = ' + '.join([f'f_{{{n}}}(i)' for n in factor_names]) if factor_names else r'\sum_k f_k(i)'
        composite_latex = (
            r'\text{score}(i) = \frac{1}{' + str(N or 'N') + r'}\left(' + inside + r'\right)'
            r',\qquad \text{signal}(i) = \text{CrossSectionRank}\big(\text{score}(i)\big)'
        )
        if is_mean_rev:
            composite_latex += (
                r',\;\text{Reversion: } \text{signal} \leftarrow 1 - \text{signal}'
                if lang == 'en' else
                r',\;\text{反转: } \text{signal} \leftarrow 1 - \text{signal}'
            )

        motivation = A['body'].format(N=N or 1, w=1.0 / N if N else 0.0)
        if is_mean_rev:
            motivation += A['reversion_note']

        return {
            'account_id': account_id, 'group': 'A',
            'strategy_name': strategy_name, 'factors': factors,
            'composite': {
                'latex': composite_latex, 'weights': 'equal',
                'weight_value': round(1.0 / N, 4) if N else None,
                'aggregation': 'mean_then_rank', 'n_factors': N,
                'motivation': motivation,
            },
        }

    # ==================== Q group (Qlib ML models) ====================
    if group == 'Q':
        # Strip CN's 'C' prefix to look up base Q-strategy (CQ01 → Q01).
        base_id = account_id.lstrip('C') if account_id.startswith('C') else account_id
        q_cfg = _QLIB_BY_BASEID.get(base_id)
        if q_cfg is not None:
            description = q_cfg.desc('en' if lang == 'en' else 'zh')
        else:
            description = meta.get('description', '') or ''
        # `factors_str` looks like 'qlib_Q08_score (Transformer)'.
        model_class = ''
        if '(' in factors_str and factors_str.endswith(')'):
            model_class = factors_str.rsplit('(', 1)[1][:-1].strip()
        composite_latex = (
            r'\text{score}(i) = M_{' + (model_class or 'qlib') + r'}'
            r'\big(\text{Alpha158/360}(i)\big)'
            r',\qquad \text{signal}(i) = \text{CrossSectionRank}\big(\text{score}(i)\big)'
        )
        return {
            'account_id': account_id, 'group': 'Q',
            'strategy_name': strategy_name,
            'model_class': model_class,
            'factors': [],   # no per-factor breakdown — the model IS the alpha
            'note': (
                'Qlib daily-retrained model. Score column: '
                f'qlib_{account_id.lstrip("C")}_score in factor_values.'
            ),
            'composite': {
                'latex': composite_latex,
                'weights': 'learned',
                'aggregation': 'model_predict_then_rank',
                'n_factors': 158 if model_class in ('LightGBM', 'XGBoost', 'CatBoost', 'Ridge', 'MLP') else 360,
                'motivation': description,
            },
        }

    # ==================== B group ====================
    gp_params = _gp_param_breakdown(factors_str, lang=lang)
    mined = _load_gp_alphas(account_id)

    VAR_DESC = GP_VAR_DESC_EN if lang == 'en' else GP_VAR_DESC_ZH

    factors = []
    for alpha in mined:
        expr = alpha.get('expression', '')
        latex = gp_expr_to_math(expr)
        vars_used = _expr_vars_used(expr)
        vars_desc = [
            {'name': v, 'latex': GP_VAR_MATH.get(v, v), 'desc': VAR_DESC.get(v, v)}
            for v in vars_used
        ]
        expl = gp_explain(expr, lang=lang)
        factors.append({
            'name': alpha.get('name', ''),
            's_expression': expr,
            'latex': latex,
            'fitness': alpha.get('fitness'),
            'ic': alpha.get('ic'),
            'vars_used': vars_desc,
            'intuition': expl['intuition'],
            'motivation': expl['motivation'],
            'alpha_source': expl['alpha_source'],
            'warnings': expl['warnings'],
        })

    n_mined = len(factors)
    if n_mined >= 2:
        composite_latex = (
            r'\text{score}(i) = \frac{1}{' + str(n_mined) + r'}\sum_{k=1}^{' + str(n_mined) + r'} f^{GP}_k(i)'
            r',\qquad \text{signal}(i) = \text{CrossSectionRank}(\text{score})'
        )
        n_factors_note = n_mined
    else:
        composite_latex = (
            r'\text{score}(i) = f^{GP}(i)'
            r',\qquad \text{signal}(i) = \text{CrossSectionRank}(\text{score})'
        )
        n_factors_note = 1

    B = _B_MOTIVATION[lang]
    if n_mined >= 2:
        motivation = B['multi'].format(n=n_mined)
    elif n_mined == 1:
        motivation = B['single']
    else:
        motivation = B['none']

    return {
        'account_id': account_id, 'group': 'B',
        'gp_info': factors_str,
        'gp_params': gp_params,
        'factors': factors,
        'note': B['note'],
        'composite': {
            'latex': composite_latex,
            'weights': 'equal' if n_mined >= 2 else 'single_expression',
            'aggregation': 'gp_expression_then_rank',
            'n_factors': n_factors_note,
            'motivation': motivation,
        },
    }
