FACTOR_FORMULAS = {
    'KMID': '(Close - Open) / Open',
    'KLEN': '(High - Low) / Open',
    'KMID2': '(Close - Open) / (High - Low)',
    'KUP': '(High - max(Open, Close)) / Open',
    'KUP2': '(High - max(Open, Close)) / (High - Low)',
    'KLOW': '(min(Open, Close) - Low) / Open',
    'KLOW2': '(min(Open, Close) - Low) / (High - Low)',
    'KSFT': '(2*Close - High - Low) / Open',
    'KSFT2': '(2*Close - High - Low) / (High - Low)',
    'ROC_5': 'Close(t) / Close(t-5) - 1',
    'ROC_10': 'Close(t) / Close(t-10) - 1',
    'ROC_20': 'Close(t) / Close(t-20) - 1',
    'MA_RATIO_5': 'Close(t) / SMA(Close, 5)',
    'MA_RATIO_10': 'Close(t) / SMA(Close, 10)',
    'MA_RATIO_20': 'Close(t) / SMA(Close, 20)',
    'VMOM_5': 'Volume(t) / SMA(Volume, 5)',
    'VMOM_10': 'Volume(t) / SMA(Volume, 10)',
    'VMOM_20': 'Volume(t) / SMA(Volume, 20)',
    'VSTD_5': 'Std(Volume, 5) / SMA(Volume, 5)',
    'VSTD_10': 'Std(Volume, 10) / SMA(Volume, 10)',
    'VSTD_20': 'Std(Volume, 20) / SMA(Volume, 20)',
    'STD_5': 'Std(Close, 5) / Close',
    'STD_10': 'Std(Close, 10) / Close',
    'STD_20': 'Std(Close, 20) / Close',
    'BBPOS_5': '(Close - SMA(Close,5)) / (2 * Std(Close,5))',
    'BBPOS_10': '(Close - SMA(Close,10)) / (2 * Std(Close,10))',
    'BBPOS_20': '(Close - SMA(Close,20)) / (2 * Std(Close,20))',
    'RSV': '(Close - Min(Low,9)) / (Max(High,9) - Min(Low,9))',
    'RSI_14': '100 - 100 / (1 + SMA(Gain,14) / SMA(Loss,14))',
    'BETA_5': 'OLS_Slope(Close, 5) / SMA(Close, 5)',
    'BETA_10': 'OLS_Slope(Close, 10) / SMA(Close, 10)',
    'BETA_20': 'OLS_Slope(Close, 20) / SMA(Close, 20)',
}

FACTOR_LATEX = {
    'KMID': r'\text{KMID} = \frac{C - O}{O}',
    'KLEN': r'\text{KLEN} = \frac{H - L}{O}',
    'KMID2': r'\text{KMID2} = \frac{C - O}{H - L}',
    'KUP': r'\text{KUP} = \frac{H - \max(O, C)}{O}',
    'KUP2': r'\text{KUP2} = \frac{H - \max(O, C)}{H - L}',
    'KLOW': r'\text{KLOW} = \frac{\min(O, C) - L}{O}',
    'KLOW2': r'\text{KLOW2} = \frac{\min(O, C) - L}{H - L}',
    'KSFT': r'\text{KSFT} = \frac{2C - H - L}{O}',
    'KSFT2': r'\text{KSFT2} = \frac{2C - H - L}{H - L}',
    'ROC_5': r'\text{ROC}_5 = \frac{P_t}{P_{t-5}} - 1',
    'ROC_10': r'\text{ROC}_{10} = \frac{P_t}{P_{t-10}} - 1',
    'ROC_20': r'\text{ROC}_{20} = \frac{P_t}{P_{t-20}} - 1',
    'MA_RATIO_5': r'\text{MA\_RATIO}_5 = \frac{P_t}{\bar{P}_5}',
    'MA_RATIO_10': r'\text{MA\_RATIO}_{10} = \frac{P_t}{\bar{P}_{10}}',
    'MA_RATIO_20': r'\text{MA\_RATIO}_{20} = \frac{P_t}{\bar{P}_{20}}',
    'VMOM_5': r'\text{VMOM}_5 = \frac{V_t}{\bar{V}_5}',
    'VMOM_10': r'\text{VMOM}_{10} = \frac{V_t}{\bar{V}_{10}}',
    'VMOM_20': r'\text{VMOM}_{20} = \frac{V_t}{\bar{V}_{20}}',
    'VSTD_5': r'\text{VSTD}_5 = \frac{\sigma(V, 5)}{\bar{V}_5}',
    'VSTD_10': r'\text{VSTD}_{10} = \frac{\sigma(V, 10)}{\bar{V}_{10}}',
    'VSTD_20': r'\text{VSTD}_{20} = \frac{\sigma(V, 20)}{\bar{V}_{20}}',
    'STD_5': r'\text{STD}_5 = \frac{\sigma(P, 5)}{P}',
    'STD_10': r'\text{STD}_{10} = \frac{\sigma(P, 10)}{P}',
    'STD_20': r'\text{STD}_{20} = \frac{\sigma(P, 20)}{P}',
    'BBPOS_5': r'\text{BB}_5 = \frac{P - \bar{P}_5}{2\sigma_5}',
    'BBPOS_10': r'\text{BB}_{10} = \frac{P - \bar{P}_{10}}{2\sigma_{10}}',
    'BBPOS_20': r'\text{BB}_{20} = \frac{P - \bar{P}_{20}}{2\sigma_{20}}',
    'RSV': r'\text{RSV} = \frac{C - L_9}{H_9 - L_9}',
    'RSI_14': r'\text{RSI}_{14} = 100 - \frac{100}{1 + \frac{\text{SMA}(\Delta^+, 14)}{\text{SMA}(\Delta^-, 14)}}',
    'BETA_5': r'\beta_5 = \frac{\hat{b}_5}{\bar{P}_5}',
    'BETA_10': r'\beta_{10} = \frac{\hat{b}_{10}}{\bar{P}_{10}}',
    'BETA_20': r'\beta_{20} = \frac{\hat{b}_{20}}{\bar{P}_{20}}',
}

FACTOR_EXPLANATIONS = {
    # Each entry: {'physics': 数学/物理直觉, 'motivation': 交易动机——这个信号在说什么市场行为}
    'KMID': {
        'physics': '数学上 (C-O)/O 是收盘相对开盘的归一化位移，类似弹簧振子 x = (终态-初态)/初态。',
        'motivation': '告诉我们当日买卖双方最终谁赢了。KMID>0 说明日内买方把价格从开盘推到了更高的收盘，是典型的"日内多头胜利"标签——第二天倾向于延续这种方向（日内动量溢出效应）。',
    },
    'KLEN': {
        'physics': '(H-L)/O 是归一化日振幅，对应谐振子的振幅 A。',
        'motivation': '衡量当日的"情绪幅度"。振幅大说明当天有大新闻或分歧剧烈，通常伴随趋势启动或反转；振幅小说明市场平静、流动性低——两种极端对应完全不同的交易逻辑。',
    },
    'KMID2': {
        'physics': '(C-O)/(H-L)，把日收益用振幅归一化，类似信噪比：有效位移/总幅度。',
        'motivation': '|KMID2|≈1 表示"单边行情"，从开盘直接走到最高/最低收盘，多空一方压倒性胜利；|KMID2|≈0 表示"十字星"，多空缠斗无果。单边日往往预示次日延续，十字星预示反转或震荡。',
    },
    'KUP': {
        'physics': '上影线 (H - max(O,C))/O。可视为"未能守住"的那部分向上动能。',
        'motivation': '长上影 = 当日价格曾冲高但被卖压打回来，暗示上方存在大量获利盘或抛压，这是看跌信号（"射击之星"形态的核心）。',
    },
    'KUP2': {
        'physics': '上影线/振幅比，把上影归一化到 [0, 1]。',
        'motivation': '同 KUP 但不受波动率影响，跨股票可比。值大说明即便算上全天波动，上方阻力依然显著。',
    },
    'KLOW': {
        'physics': '下影线 (min(O,C) - L)/O，未能击穿的向下动能。',
        'motivation': '长下影 = 价格曾下跌但被接回，暗示下方有资金托底（"锤子线"）。经典看涨反转信号。',
    },
    'KLOW2': {
        'physics': '下影线/振幅比，归一化到 [0, 1]。',
        'motivation': '跨股票可比的支撑强度指标。和 KUP2 一起用可以识别"上下都有承接"的 coiling 形态。',
    },
    'KSFT': {
        'physics': '(2C-H-L)/O，等价于收盘在日内中位数之上的距离，类比质心坐标。',
        'motivation': '把"收盘位置"这件事和"开盘点位"一起标准化。>0 表示收盘在日内中上部（买方控场），<0 在下部（卖方控场），捕捉的是日内的长期力量对比。',
    },
    'KSFT2': {
        'physics': '(2C-H-L)/(H-L)，归一化到 [-1, 1]。',
        'motivation': '收盘位置在日内区间的百分比位置。+1 收在最高、-1 收在最低。跨股票比较"尾盘强度"的标准化工具。',
    },
    'ROC_5': {
        'physics': '5 日收益率 (P_t/P_{t-5} - 1)，价格的离散一阶导数（速度）。',
        'motivation': '短期动量：过去一周涨的股票下周大概率继续涨（Jegadeesh-Titman 1993 动量效应）。原因是信息扩散、追涨行为、机构调仓需要时间。',
    },
    'ROC_10': {
        'physics': '10 日收益率，中频速度估计。',
        'motivation': '双周动量。比 5 日更稳，受日噪声影响小。用于过滤短线假突破。',
    },
    'ROC_20': {
        'physics': '20 日收益率，月度速度。',
        'motivation': '中期趋势指标。财报季、机构季度调仓、行业主题轮动的时间尺度。',
    },
    'MA_RATIO_5': {
        'physics': 'P_t / SMA(P, 5)，价格相对 5 日均线的比值，近似弹簧回复力 F=-k(x-x₀)。',
        'motivation': '短期"偏离中枢多远"。>1.05 = 急涨超买，短期容易回调；<0.95 = 急跌超卖，短期容易反弹。是均值回归流派的核心信号。',
    },
    'MA_RATIO_10': {
        'physics': 'P_t / SMA(P, 10)。',
        'motivation': '中期相对位置。判断股票现在是在自己的"正常水位"还是偏离太远——机构建仓通常避免买在过高 MA 比的股票上。',
    },
    'MA_RATIO_20': {
        'physics': 'P_t / SMA(P, 20)。',
        'motivation': '月线相对偏离。>1 = 处于月线上方趋势向好，<1 = 月线下方弱势。常作为趋势过滤器：只在 >1 时做多动量因子。',
    },
    'VMOM_5': {
        'physics': 'V_t / V̄_5，成交量相对 5 日均量的比值。',
        'motivation': '放量/缩量的识别。放量上涨 = 有资金真金白银进场，可信度高；缩量上涨 = 没人接盘，警惕假突破。量价关系是老牌技术分析的核心。',
    },
    'VMOM_10': {
        'physics': 'V_t / V̄_10。',
        'motivation': '两周量能变化。识别机构是否在持续吸筹。',
    },
    'VMOM_20': {
        'physics': 'V_t / V̄_20。',
        'motivation': '月度量能基线。突破历史均量往往对应重大事件（财报、新闻、主题爆发）。',
    },
    'VSTD_5': {
        'physics': 'σ(V, 5) / V̄_5，量能变异系数。',
        'motivation': '量能稳定性。VSTD 大说明成交量忽高忽低——对应热点股、炒作股；VSTD 小说明量能稳定——机构持股、大盘蓝筹。风险偏好维度。',
    },
    'VSTD_10': {
        'physics': 'σ(V, 10) / V̄_10。',
        'motivation': '中期量能波动。高值 = 关注度剧烈变化中的股票，常伴随基本面催化。',
    },
    'VSTD_20': {
        'physics': 'σ(V, 20) / V̄_20。',
        'motivation': '长期"关注度稳定性"指标。低值股票通常是冷门、容量大的价值股。',
    },
    'STD_5': {
        'physics': 'σ(P, 5) / P，归一化价格波动率，对应布朗运动扩散系数。',
        'motivation': '短期风险。volatility 本身就是一个定价因子（低波异象：Ang et al. 2006 发现低波股票长期收益反而更高）。',
    },
    'STD_10': {
        'physics': 'σ(P, 10) / P。',
        'motivation': '双周风险。Black-Scholes 模型里直接作为期权定价输入，机构对这个值敏感。',
    },
    'STD_20': {
        'physics': 'σ(P, 20) / P。',
        'motivation': '月度风险。低波动组合长期跑赢高波动组合（low-volatility anomaly）。',
    },
    'BBPOS_5': {
        'physics': '(P - SMA_5) / (2σ_5)，布林带内的 z-score。',
        'motivation': '"你现在偏离均值几个标准差"。BBPOS>1 极度超买，<-1 极度超卖。假设价格服从正态分布时，|BBPOS|>1 发生概率仅 32%，是统计套利的经典触发条件。',
    },
    'BBPOS_10': {
        'physics': '(P - SMA_10) / (2σ_10)。',
        'motivation': '中期 z-score，过滤短期噪声后的均值回归触发器。',
    },
    'BBPOS_20': {
        'physics': '(P - SMA_20) / (2σ_20)。',
        'motivation': '月度 z-score。Ornstein-Uhlenbeck 过程（均值回归过程）的标准刻画。量化对冲基金常用此信号做配对交易。',
    },
    'RSV': {
        'physics': '(C - L_9) / (H_9 - L_9)，价格在过去 9 日 [L, H] 区间内的百分位。',
        'motivation': '回答"现在价格在过去两周区间的什么位置"。=1 刚创新高，=0 刚创新低。是 KD 指标的分子，捕捉的是"短期极端"。',
    },
    'RSI_14': {
        'physics': '100 - 100/(1 + avg(上涨幅度)/avg(下跌幅度))，涨跌动能比的归一化。',
        'motivation': '衡量"多头/空头哪方在近期更卖力"。>70 传统认为超买，<30 超卖。但在强趋势中这个阈值会失灵——所以 RSI 更适合和 BBPOS/STD 组合使用。',
    },
    'BETA_5': {
        'physics': 'OLS 斜率 / 均价，即过去 5 日价格线性拟合的斜率（归一化）。是"速度"的稳健版本——ROC 只看两个端点，BETA 用全部点。',
        'motivation': 'ROC_5 看的是首尾两天的差，容易被单日异常值扭曲；BETA_5 用 5 天所有点拟合一条直线，衡量"持续性趋势"，对噪声更鲁棒。机构更偏爱 BETA 类因子。',
    },
    'BETA_10': {
        'physics': 'OLS 斜率 / 均价，10 日窗口。',
        'motivation': '中期趋势强度的稳健估计。和 ROC_10 相关但更干净。',
    },
    'BETA_20': {
        'physics': 'OLS 斜率 / 均价，20 日窗口。',
        'motivation': '月度趋势的可信估计。20 个数据点已足够使 OLS 稳定，是很多量化模型的骨干趋势因子。',
    },
}

FEATURE_COLS = ['o_c', 'h_c', 'l_c', 'v_vma20', 'ma_5', 'ma_10', 'ma_20', 'std_5', 'std_10', 'std_20', 'ret_1', 'ret_5', 'ret_10']

GP_FUNC_MATH = {
    'add': '({0} + {1})',
    'sub': '({0} - {1})',
    'mul': '({0} \\times {1})',
    'div': '\\frac{{{0}}}{{{1}}}',
    'sqrt_abs': '\\sqrt{{|{0}|}}',
    'log_abs1': '\\ln(|{0}| + 1)',
    'neg': '-({0})',
    'inv': '\\frac{{1}}{{{0}}}',
    'max2': '\\max({0}, {1})',
    'min2': '\\min({0}, {1})',
}

GP_VAR_MATH = {
    'o_c': '\\frac{O-C}{C}',
    'h_c': '\\frac{H-C}{C}',
    'l_c': '\\frac{L-C}{C}',
    'v_vma20': '\\frac{V}{\\bar{V}_{20}}',
    'ma_5': '\\frac{\\bar{C}_5}{C}',
    'ma_10': '\\frac{\\bar{C}_{10}}{C}',
    'ma_20': '\\frac{\\bar{C}_{20}}{C}',
    'std_5': '\\frac{\\sigma_5}{C}',
    'std_10': '\\frac{\\sigma_{10}}{C}',
    'std_20': '\\frac{\\sigma_{20}}{C}',
    'ret_1': 'r_1',
    'ret_5': 'r_5',
    'ret_10': 'r_{10}',
}

# Arity of each GP function
_ARITY = {
    'add': 2, 'sub': 2, 'mul': 2, 'div': 2,
    'max2': 2, 'min2': 2,
    'sqrt_abs': 1, 'log_abs1': 1, 'neg': 1, 'inv': 1,
}

def gp_expr_to_math(expr_str: str) -> str:
    """Convert a gplearn expression like 'max2(X11, log_abs1(X10))' to LaTeX."""
    if not expr_str:
        return ''
    # Tokenize: names, numbers, (, ), comma
    import re as _re
    tokens = _re.findall(r'[A-Za-z_][A-Za-z0-9_]*|-?\d+\.?\d*|[(),]', expr_str)
    pos = 0

    def parse():
        nonlocal pos
        if pos >= len(tokens):
            return '?'
        tok = tokens[pos]
        pos += 1
        # Identifier — could be function call or variable
        if _re.match(r'[A-Za-z_]', tok):
            # Peek for '(' → function call
            if pos < len(tokens) and tokens[pos] == '(':
                pos += 1  # consume '('
                args = []
                if pos < len(tokens) and tokens[pos] != ')':
                    args.append(parse())
                    while pos < len(tokens) and tokens[pos] == ',':
                        pos += 1
                        args.append(parse())
                if pos < len(tokens) and tokens[pos] == ')':
                    pos += 1
                fmt = GP_FUNC_MATH.get(tok)
                if fmt:
                    try:
                        return fmt.format(*args)
                    except Exception:
                        return tok + '(' + ', '.join(args) + ')'
                return tok + '(' + ', '.join(args) + ')'
            # Variable
            if tok in GP_VAR_MATH:
                return GP_VAR_MATH[tok]
            if tok.startswith('X'):
                try:
                    idx = int(tok[1:])
                    if idx < len(FEATURE_COLS):
                        return GP_VAR_MATH.get(FEATURE_COLS[idx], tok)
                except ValueError:
                    pass
            return tok
        # Numeric
        try:
            v = float(tok)
            return f'{v:.3g}'
        except ValueError:
            return tok

    try:
        return parse()
    except Exception:
        return expr_str
