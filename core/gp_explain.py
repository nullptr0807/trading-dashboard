"""Per-expression explainer for GP-evolved factor trees.

Given a gplearn expression string, produce four fields:
  - intuition  : what the expression is computing
  - motivation : design / trading motivation
  - alpha_source : why it might generate excess returns (anomaly grounding)
  - warnings   : bloat / redundancy warnings

Now language-aware via `explain(expr, lang='en'|'zh')`.
"""
from __future__ import annotations
import re
from core.factor_formulas import FEATURE_COLS
from core.factor_formulas_en import VAR_META_EN, ALPHA_REASONS_EN

# ------ AST -----------------------------------------------------------------

class Node:
    __slots__ = ('kind', 'name', 'children', 'value')
    def __init__(self, kind, name=None, children=None, value=None):
        self.kind = kind
        self.name = name
        self.children = children or []
        self.value = value

    def __eq__(self, other):
        if not isinstance(other, Node):
            return False
        if self.kind != other.kind:
            return False
        if self.kind == 'num':
            return self.value == other.value
        if self.kind == 'var':
            return self.name == other.name
        return self.name == other.name and len(self.children) == len(other.children) and all(a == b for a, b in zip(self.children, other.children))

    def __hash__(self):
        if self.kind == 'num':
            return hash(('n', self.value))
        if self.kind == 'var':
            return hash(('v', self.name))
        return hash(('c', self.name, tuple(hash(c) for c in self.children)))


def parse(expr: str) -> Node | None:
    tokens = re.findall(r'[A-Za-z_][A-Za-z0-9_]*|-?\d+\.?\d*|[(),]', expr or '')
    pos = [0]

    def _parse():
        if pos[0] >= len(tokens):
            return None
        tok = tokens[pos[0]]; pos[0] += 1
        if re.match(r'[A-Za-z_]', tok):
            if pos[0] < len(tokens) and tokens[pos[0]] == '(':
                pos[0] += 1
                args = []
                if pos[0] < len(tokens) and tokens[pos[0]] != ')':
                    args.append(_parse())
                    while pos[0] < len(tokens) and tokens[pos[0]] == ',':
                        pos[0] += 1
                        args.append(_parse())
                if pos[0] < len(tokens) and tokens[pos[0]] == ')':
                    pos[0] += 1
                return Node('call', tok, args)
            if tok.startswith('X'):
                try:
                    i = int(tok[1:])
                    if i < len(FEATURE_COLS):
                        return Node('var', FEATURE_COLS[i])
                except ValueError:
                    pass
            return Node('var', tok)
        try:
            return Node('num', value=float(tok))
        except ValueError:
            return Node('var', tok)

    try:
        return _parse()
    except Exception:
        return None


# ------ Simplification ------------------------------------------------------

_WARN_STRINGS = {
    'zh': {
        'nested': '检测到冗余嵌套 {name}(x, {name}(x, y)) = {name}(x, y)——GP bloat',
        'nested2': '检测到冗余嵌套——GP bloat',
        'sqrt_stack': '√ 叠了 {d} 层 → 等价于 |x|^(1/{p})，幅度被极度压缩。常见 GP bloat。',
    },
    'en': {
        'nested': 'Redundant nesting detected: {name}(x, {name}(x, y)) = {name}(x, y) — GP bloat',
        'nested2': 'Redundant nesting detected — GP bloat',
        'sqrt_stack': 'Stacked {d} √ layers → equivalent to |x|^(1/{p}); magnitude extremely compressed. Common GP bloat.',
    },
}


def simplify(n: Node, lang: str = 'zh') -> tuple[Node, list[str]]:
    warnings = []
    W = _WARN_STRINGS.get(lang, _WARN_STRINGS['zh'])

    def _rec(node: Node) -> Node:
        if node.kind != 'call':
            return node
        kids = [_rec(c) for c in node.children]
        node2 = Node('call', node.name, kids)
        if node.name in ('max2', 'min2') and len(kids) == 2:
            a, b = kids
            if b.kind == 'call' and b.name == node.name:
                if a in b.children:
                    warnings.append(W['nested'].format(name=node.name))
                    return b
            if a.kind == 'call' and a.name == node.name:
                if b in a.children:
                    warnings.append(W['nested2'])
                    return a
        if node.name == 'sqrt_abs' and len(kids) == 1 and kids[0].kind == 'call' and kids[0].name == 'sqrt_abs':
            depth = 1
            inner = kids[0]
            while inner.kind == 'call' and inner.name == 'sqrt_abs':
                depth += 1
                inner = inner.children[0] if inner.children else inner
                if depth > 5:
                    break
            warnings.append(W['sqrt_stack'].format(d=depth, p=2**depth))
        return node2

    return _rec(n), warnings


# ------ Variable categorization --------------------------------------------

_VAR_META_ZH = {
    'o_c':     ('日内形态', '开盘相对收盘的位置'),
    'h_c':     ('日内形态', '最高价相对收盘'),
    'l_c':     ('日内形态', '最低价相对收盘'),
    'v_vma20': ('量能',     '成交量相对 20 日均量'),
    'ma_5':    ('均线位置', '5 日均线相对当前价'),
    'ma_10':   ('均线位置', '10 日均线相对当前价'),
    'ma_20':   ('均线位置', '20 日均线相对当前价'),
    'std_5':   ('波动率',   '5 日价格波动率'),
    'std_10':  ('波动率',   '10 日价格波动率'),
    'std_20':  ('波动率',   '20 日价格波动率'),
    'ret_1':   ('动量',     '昨日收益率'),
    'ret_5':   ('动量',     '5 日收益率'),
    'ret_10':  ('动量',     '10 日收益率'),
}

_ALPHA_REASONS_ZH = {
    '动量':     '动量延续效应（Jegadeesh-Titman 1993）：过去涨得好的股票短期倾向继续涨，原因是信息扩散慢、机构调仓需时间、散户追涨。',
    '波动率':   '低波异象（Ang et al. 2006）：低波动股票长期收益反而更高；或波动率聚集（Engle ARCH）：高波刚发生时，次日大概率继续异常。',
    '均线位置': '均值回归（Ornstein-Uhlenbeck 过程）：价格偏离长期均衡时存在回复力，过远的偏离通常伴随反转。',
    '量能':     '量价关系：异常放量通常对应信息事件（财报、新闻），为价格变化提供可信度确认。',
    '日内形态': '日内微结构信号：开盘-收盘-最高-最低的相对位置反映了盘中多空博弈的胜负方，带有次日动量延续效应。',
}


def _lang_tables(lang: str):
    if lang == 'en':
        return VAR_META_EN, ALPHA_REASONS_EN
    return _VAR_META_ZH, _ALPHA_REASONS_ZH


def _categories(node: Node, var_meta) -> set:
    cats = set()
    if node.kind == 'var' and node.name in var_meta:
        cats.add(var_meta[node.name][0])
    for c in node.children or []:
        cats |= _categories(c, var_meta)
    return cats


def _variables(node: Node, var_meta) -> list:
    out = []
    def walk(n):
        if n.kind == 'var' and n.name in var_meta and n.name not in out:
            out.append(n.name)
        for c in n.children or []:
            walk(c)
    walk(node)
    return out


def _describe_node(n: Node, var_meta) -> str:
    if n.kind == 'num':
        return f'{n.value:g}'
    if n.kind == 'var':
        meta = var_meta.get(n.name)
        return meta[1] if meta else n.name
    args = [_describe_node(c, var_meta) for c in n.children]
    name = n.name
    if name == 'add':     return f'({args[0]} + {args[1]})'
    if name == 'sub':     return f'({args[0]} − {args[1]})'
    if name == 'mul':     return f'({args[0]} × {args[1]})'
    if name == 'div':     return f'({args[0]} ÷ {args[1]})'
    if name == 'neg':     return f'−({args[0]})'
    if name == 'inv':     return f'1/({args[0]})'
    if name == 'max2':    return f'max({args[0]}, {args[1]})'
    if name == 'min2':    return f'min({args[0]}, {args[1]})'
    if name == 'sqrt_abs':return f'√|{args[0]}|'
    if name == 'log_abs1':return f'ln(|{args[0]}|+1)'
    return f'{name}({", ".join(args)})'


# ------ Intuition templates ------------------------------------------------

_I18N = {
    'zh': {
        'parse_fail': '无法解析表达式。',
        'single_var': '直接使用单变量：{plain}。没有任何变换，原始值就是打分。',
        'unary_compress': '对 {vdesc} 取绝对值后做{op}压缩：{plain}。丢弃了方向信息，只保留"幅度大小"，并把尾部极端值往中心拉近。',
        'op_sqrt': '平方根',
        'op_log': '对数',
        'max2': '取 {a} 和 {b} 两者中较大的那个：{plain}。任意一边出大信号就会被选中——对"触发过强信号"敏感，对两边同时弱的情况无响应。',
        'min2': '取 {a} 和 {b} 两者中较小的那个：{plain}。只有两者同时都不小时才会输出大值，是"稳健一致"的过滤器。',
        'add': '把两个信号线性相加：{plain}。同方向会增强，相反方向会抵消。',
        'sub': '两个信号相减：{plain}。度量的是"差值"，即两者背离程度。',
        'mul': '两个信号相乘：{plain}。同号时输出正数（共振），异号时输出负数，能够放大"两个条件都强"的情形。',
        'div': '两信号相除：{plain}。分子的相对强度（归一化到分母尺度）。',
        'complex': '复合表达式：{plain}。',
        'one_cat': '整个表达式全部建立在"{cat}"这一个维度上（仅用变量：{vars}）。GP 等价于从{cat}这一类信号里搜索最优的非线性函数形式。',
        'multi_cat': '跨越 {n} 个市场维度：{cats}（变量：{vars}）。动机是把不同维度的弱信号组合起来——多维信号的交互常常产生比单因子更稳健的 alpha。',
        'no_cat': '未识别到已知特征变量。',
        'addon_max': ' 选 max 相当于 OR 逻辑——只要任一条件触发就算"信号存在"，适合捕捉"尾部事件"（罕见但影响大）。',
        'addon_min': ' 选 min 相当于 AND 逻辑——要求多个条件同时成立，适合提高精确率、降低假信号。',
        'addon_compress': ' {op}压缩把极端值向中心拉，能降低异常股票对因子分布的拖累（类似 Winsorization 的平滑版本）。',
        'no_alpha': '• 未匹配到已知学术异象——这可能是真正的新因子，也可能是过拟合。需要样本外验证。',
        'cat_sep': '、',
    },
    'en': {
        'parse_fail': 'Unable to parse expression.',
        'single_var': 'Uses a single variable directly: {plain}. No transformation — the raw value is the score.',
        'unary_compress': 'Takes |{vdesc}| then applies {op} compression: {plain}. Direction is discarded, only magnitude is kept, and extreme tails are pulled toward the center.',
        'op_sqrt': 'square-root',
        'op_log': 'logarithmic',
        'max2': 'Picks the larger of {a} and {b}: {plain}. Either side firing is enough — sensitive to any strong trigger, unresponsive when both sides are weak.',
        'min2': 'Picks the smaller of {a} and {b}: {plain}. Only outputs a large value when both are non-small — a "robust consensus" filter.',
        'add': 'Linear sum of two signals: {plain}. Same-direction signals reinforce; opposing ones cancel.',
        'sub': 'Difference of two signals: {plain}. Measures divergence between the two.',
        'mul': 'Product of two signals: {plain}. Same sign → positive (resonance); opposite sign → negative. Amplifies "both conditions strong" cases.',
        'div': 'Ratio of two signals: {plain}. Relative strength of numerator (normalized by the denominator\'s scale).',
        'complex': 'Composite expression: {plain}.',
        'one_cat': 'The whole expression lives in a single dimension: "{cat}" (variables used: {vars}). GP is searching for the best nonlinear functional form within this family of signals.',
        'multi_cat': 'Spans {n} market dimensions: {cats} (variables: {vars}). Combining weak signals from different dimensions often produces alpha more robust than any single factor.',
        'no_cat': 'No known feature variables detected.',
        'addon_max': ' Using max is OR-logic — any condition firing counts as "signal present"; good for capturing tail events (rare but impactful).',
        'addon_min': ' Using min is AND-logic — requires multiple conditions simultaneously; improves precision and reduces false signals.',
        'addon_compress': ' {op} compression pulls extreme values toward the center, reducing the drag of outlier stocks on the factor distribution (a smoothed Winsorization).',
        'no_alpha': '• No match to known academic anomalies — could be a genuinely new factor, or overfitting. Needs out-of-sample validation.',
        'cat_sep': ', ',
    },
}


def explain(expr: str, lang: str = 'zh') -> dict:
    if lang not in _I18N:
        lang = 'zh'
    L = _I18N[lang]
    var_meta, alpha_reasons = _lang_tables(lang)

    node = parse(expr)
    if node is None:
        return {
            'intuition': L['parse_fail'],
            'motivation': '',
            'alpha_source': '',
            'warnings': [],
        }
    simplified, warnings = simplify(node, lang=lang)
    cats = _categories(simplified, var_meta)
    vars_ = _variables(simplified, var_meta)
    plain = _describe_node(simplified, var_meta)

    root_name = simplified.name if simplified.kind == 'call' else None
    if simplified.kind == 'var':
        intuition = L['single_var'].format(plain=plain)
    elif root_name in ('sqrt_abs', 'log_abs1') and len(simplified.children) == 1 and simplified.children[0].kind == 'var':
        vname = simplified.children[0].name
        vdesc = var_meta.get(vname, (None, vname))[1]
        op = L['op_sqrt'] if root_name == 'sqrt_abs' else L['op_log']
        intuition = L['unary_compress'].format(vdesc=vdesc, op=op, plain=plain)
    elif root_name == 'max2':
        a, b = simplified.children
        intuition = L['max2'].format(a=_describe_node(a, var_meta), b=_describe_node(b, var_meta), plain=plain)
    elif root_name == 'min2':
        a, b = simplified.children
        intuition = L['min2'].format(a=_describe_node(a, var_meta), b=_describe_node(b, var_meta), plain=plain)
    elif root_name == 'add':
        intuition = L['add'].format(plain=plain)
    elif root_name == 'sub':
        intuition = L['sub'].format(plain=plain)
    elif root_name == 'mul':
        intuition = L['mul'].format(plain=plain)
    elif root_name == 'div':
        intuition = L['div'].format(plain=plain)
    else:
        intuition = L['complex'].format(plain=plain)

    if len(cats) == 1:
        cat = next(iter(cats))
        motivation = L['one_cat'].format(cat=cat, vars=', '.join(vars_))
    elif len(cats) >= 2:
        motivation = L['multi_cat'].format(n=len(cats), cats=L['cat_sep'].join(cats), vars=', '.join(vars_))
    else:
        motivation = L['no_cat']

    if root_name == 'max2':
        motivation += L['addon_max']
    if root_name == 'min2':
        motivation += L['addon_min']
    if root_name in ('sqrt_abs', 'log_abs1'):
        op = L['op_log'] if root_name == 'log_abs1' else L['op_sqrt']
        motivation += L['addon_compress'].format(op=op)

    reasons = []
    for cat in cats:
        if cat in alpha_reasons:
            reasons.append(f'• [{cat}] {alpha_reasons[cat]}')
    if not reasons:
        reasons.append(L['no_alpha'])
    alpha_source = '\n'.join(reasons)

    return {
        'intuition': intuition,
        'motivation': motivation,
        'alpha_source': alpha_source,
        'warnings': warnings,
    }
