"""English translations of factor explanations. Mirrors FACTOR_EXPLANATIONS in factor_formulas.py."""

FACTOR_EXPLANATIONS_EN = {
    'KMID': {
        'physics': 'Mathematically (C-O)/O is the normalized close-vs-open displacement, like a spring oscillator x = (end-start)/start.',
        'motivation': 'Tells us who ultimately won the intraday fight. KMID>0 means buyers pushed price from open to a higher close — a classic "intraday bulls won" tag that tends to carry into the next day (intraday momentum spillover).',
    },
    'KLEN': {
        'physics': '(H-L)/O is the normalized daily range, analogous to a harmonic oscillator amplitude A.',
        'motivation': 'Measures the day\'s "emotional amplitude". Large range = big news or sharp disagreement, often starting a trend or reversal; small range = calm, low liquidity — two extremes calling for completely different trading logic.',
    },
    'KMID2': {
        'physics': '(C-O)/(H-L) — daily return normalized by range. Similar to signal-to-noise: effective displacement / total range.',
        'motivation': '|KMID2|≈1 means a "one-sided day" from open straight to the highest/lowest close — one side dominated; |KMID2|≈0 is a doji, bulls and bears in stalemate. One-sided days often continue, dojis predict reversal or chop.',
    },
    'KUP': {
        'physics': 'Upper shadow (H - max(O,C))/O. Interpretable as the "couldn\'t-hold" portion of upward momentum.',
        'motivation': 'Long upper shadow = price rallied but got sold back, hinting at overhead supply. A bearish signal (core of the "shooting star" pattern).',
    },
    'KUP2': {
        'physics': 'Upper-shadow / range ratio, normalized to [0, 1].',
        'motivation': 'Same intuition as KUP but volatility-independent and cross-sectionally comparable. High value = overhead resistance is meaningful even accounting for total volatility.',
    },
    'KLOW': {
        'physics': 'Lower shadow (min(O,C) - L)/O, the "couldn\'t-break" downward momentum.',
        'motivation': 'Long lower shadow = price fell but got bought back — buyers defending a support zone ("hammer"). Classic bullish reversal signal.',
    },
    'KLOW2': {
        'physics': 'Lower-shadow / range ratio, normalized to [0, 1].',
        'motivation': 'Cross-sectionally comparable support strength. Combined with KUP2, it identifies "coiling" patterns with support on both sides.',
    },
    'KSFT': {
        'physics': '(2C-H-L)/O — equivalent to the close\'s distance above the intraday midpoint, analogous to a center-of-mass coordinate.',
        'motivation': 'Standardizes "where did it close" against the opening reference. >0 = closed in upper half (buyers controlling the tape), <0 = lower half (sellers). Captures the intraday balance of power.',
    },
    'KSFT2': {
        'physics': '(2C-H-L)/(H-L), normalized to [-1, 1].',
        'motivation': 'Percentage position of the close within the day\'s range. +1 closed at the top, -1 at the bottom. Standardized way to compare "late-session strength" across stocks.',
    },
    'ROC_5': {
        'physics': '5-day return (P_t/P_{t-5} - 1), the discrete first derivative of price (velocity).',
        'motivation': 'Short-term momentum: stocks that rose over the past week tend to continue the next week (Jegadeesh-Titman 1993 momentum effect). Causes include slow information diffusion, trend-chasing, and institutional rebalancing lag.',
    },
    'ROC_10': {
        'physics': '10-day return, a mid-frequency velocity estimate.',
        'motivation': 'Bi-weekly momentum. More stable than 5-day, less affected by daily noise. Useful for filtering short-term false breakouts.',
    },
    'ROC_20': {
        'physics': '20-day return, monthly velocity.',
        'motivation': 'Medium-term trend indicator. Timescale of earnings seasons, quarterly institutional rebalancing, and sector rotation.',
    },
    'MA_RATIO_5': {
        'physics': 'P_t / SMA(P, 5) — price relative to the 5-day mean, approximating a restoring force F = -k(x - x₀).',
        'motivation': 'How far above/below the short-term anchor. >1.05 = sharp rally, overbought, prone to pullback; <0.95 = sharp selloff, oversold, prone to bounce. Core mean-reversion signal.',
    },
    'MA_RATIO_10': {
        'physics': 'P_t / SMA(P, 10).',
        'motivation': 'Medium-term relative position. Judges whether the stock is near its "normal water level" or too far stretched — institutions usually avoid buying when this ratio is extreme.',
    },
    'MA_RATIO_20': {
        'physics': 'P_t / SMA(P, 20).',
        'motivation': 'Monthly relative deviation. >1 = above the monthly trendline (healthy); <1 = below (weak). Often used as a trend filter — only take momentum longs when >1.',
    },
    'VMOM_5': {
        'physics': 'V_t / V̄_5 — volume relative to 5-day average.',
        'motivation': 'Volume-surge detector. Rising price on expanding volume = real money entering, high conviction; rising price on shrinking volume = no buyers, watch for a false breakout. The price-volume relationship is a cornerstone of technical analysis.',
    },
    'VMOM_10': {
        'physics': 'V_t / V̄_10.',
        'motivation': 'Two-week volume change. Detects sustained accumulation by institutions.',
    },
    'VMOM_20': {
        'physics': 'V_t / V̄_20.',
        'motivation': 'Monthly volume baseline. A breakout above the long-run average often corresponds to a material event (earnings, news, thematic catalyst).',
    },
    'VSTD_5': {
        'physics': 'σ(V, 5) / V̄_5 — the coefficient of variation in volume.',
        'motivation': 'Volume stability. High VSTD = erratic volume, typical of hot-theme or speculative names; low VSTD = steady flow, typical of institutional blue-chips. A risk-appetite dimension.',
    },
    'VSTD_10': {
        'physics': 'σ(V, 10) / V̄_10.',
        'motivation': 'Medium-term volume volatility. High values identify stocks with rapidly changing attention, often driven by fundamental catalysts.',
    },
    'VSTD_20': {
        'physics': 'σ(V, 20) / V̄_20.',
        'motivation': 'Long-term "attention stability". Low-VSTD stocks are usually under-covered, high-capacity value names.',
    },
    'STD_5': {
        'physics': 'σ(P, 5) / P — normalized price volatility, analogous to a Brownian-motion diffusion coefficient.',
        'motivation': 'Short-term risk. Volatility is itself a pricing factor (low-vol anomaly: Ang et al. 2006 found low-vol stocks earn higher long-run returns).',
    },
    'STD_10': {
        'physics': 'σ(P, 10) / P.',
        'motivation': 'Bi-weekly risk. Feeds directly into Black-Scholes option pricing; institutions are sensitive to it.',
    },
    'STD_20': {
        'physics': 'σ(P, 20) / P.',
        'motivation': 'Monthly risk. Low-volatility portfolios have historically outperformed high-volatility ones (low-volatility anomaly).',
    },
    'BBPOS_5': {
        'physics': '(P - SMA_5) / (2σ_5) — the z-score within a Bollinger band.',
        'motivation': '"How many standard deviations are you from the mean." BBPOS>1 extremely overbought, <-1 extremely oversold. Under a normal-distribution assumption, |BBPOS|>1 happens only ~32% of the time — a classic statistical-arbitrage trigger.',
    },
    'BBPOS_10': {
        'physics': '(P - SMA_10) / (2σ_10).',
        'motivation': 'Mid-term z-score, a mean-reversion trigger that filters out short-term noise.',
    },
    'BBPOS_20': {
        'physics': '(P - SMA_20) / (2σ_20).',
        'motivation': 'Monthly z-score. The canonical description of an Ornstein-Uhlenbeck (mean-reverting) process. Quant hedge funds rely on this for pairs trading.',
    },
    'RSV': {
        'physics': '(C - L_9) / (H_9 - L_9) — percentile position of price within the last 9-day [L, H] range.',
        'motivation': 'Answers "where are we inside the past two-week range". =1 new high, =0 new low. The numerator of the KD indicator; captures short-term extremes.',
    },
    'RSI_14': {
        'physics': '100 - 100/(1 + avg(gain)/avg(loss)) — the normalized ratio of up-moves to down-moves.',
        'motivation': 'Quantifies which side (bulls or bears) has been working harder recently. >70 traditionally overbought, <30 oversold. But in strong trends these levels break down — RSI works best combined with BBPOS/STD.',
    },
    'BETA_5': {
        'physics': 'OLS slope / mean price — the (normalized) slope of a linear fit to the last 5 prices. A robust version of velocity: ROC uses only two endpoints, BETA uses all points.',
        'motivation': 'ROC_5 looks at only first/last day and is easily distorted by a single outlier; BETA_5 fits all 5 days with a line, measuring "persistent trend" with better noise tolerance. Institutions favor BETA-style factors.',
    },
    'BETA_10': {
        'physics': 'OLS slope / mean price, 10-day window.',
        'motivation': 'Robust estimate of medium-term trend strength. Correlated with ROC_10 but cleaner.',
    },
    'BETA_20': {
        'physics': 'OLS slope / mean price, 20-day window.',
        'motivation': 'Reliable monthly trend estimate. 20 points make the OLS stable — a backbone trend factor in many quant models.',
    },
}

# GP variable Chinese labels — English versions
GP_VAR_DESC_EN = {
    'o_c':     'Open/close ratio',
    'h_c':     'High/close ratio',
    'l_c':     'Low/close ratio',
    'v_vma20': 'Volume vs 20-day avg volume',
    'ma_5':    '5-day MA / close',
    'ma_10':   '10-day MA / close',
    'ma_20':   '20-day MA / close',
    'std_5':   '5-day volatility / price',
    'std_10':  '10-day volatility / price',
    'std_20':  '20-day volatility / price',
    'ret_1':   'Previous-day return',
    'ret_5':   '5-day return',
    'ret_10':  '10-day return',
}

# GP-variable metadata (category, description) in English
VAR_META_EN = {
    'o_c':     ('Intraday shape', 'Open relative to close'),
    'h_c':     ('Intraday shape', 'High relative to close'),
    'l_c':     ('Intraday shape', 'Low relative to close'),
    'v_vma20': ('Volume',         'Volume relative to 20-day avg volume'),
    'ma_5':    ('MA position',    '5-day MA relative to current price'),
    'ma_10':   ('MA position',    '10-day MA relative to current price'),
    'ma_20':   ('MA position',    '20-day MA relative to current price'),
    'std_5':   ('Volatility',     '5-day price volatility'),
    'std_10':  ('Volatility',     '10-day price volatility'),
    'std_20':  ('Volatility',     '20-day price volatility'),
    'ret_1':   ('Momentum',       'Previous-day return'),
    'ret_5':   ('Momentum',       '5-day return'),
    'ret_10':  ('Momentum',       '10-day return'),
}

ALPHA_REASONS_EN = {
    'Momentum':       'Momentum continuation (Jegadeesh-Titman 1993): past winners tend to keep winning short-term due to slow information diffusion, delayed institutional rebalancing, and retail trend-chasing.',
    'Volatility':     'Low-volatility anomaly (Ang et al. 2006): low-vol stocks have historically outperformed high-vol stocks; or volatility clustering (Engle ARCH): elevated volatility tends to persist the next day.',
    'MA position':    'Mean reversion (Ornstein-Uhlenbeck process): prices experience a restoring force when deviating from long-run equilibrium; large deviations often precede reversals.',
    'Volume':         'Volume-price relationship: abnormal volume usually corresponds to information events (earnings, news), providing conviction confirmation for price changes.',
    'Intraday shape': 'Intraday microstructure signal: the relative positions of open/close/high/low reflect who won the day\'s fight, carrying next-day momentum continuation.',
}

# Chinese → English category name mapping (used inside gp_explain when lang='en')
CAT_CN2EN = {
    '动量': 'Momentum',
    '波动率': 'Volatility',
    '均线位置': 'MA position',
    '量能': 'Volume',
    '日内形态': 'Intraday shape',
}
