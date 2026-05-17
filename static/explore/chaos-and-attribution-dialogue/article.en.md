# From "Can a Chaotic System Even Be Backtested?" to "All 10 Strategies Bankrupt over 4 Years": A Full Dialogue Replay

**Date**: 2026-05-06
**Category**: Methodology / Dialogue Log

![hero](hero.png)

## Why This Article

This isn't a new experiment — it's a replay of a conversation. It started with what sounded like a philosophical question, got pushed back on, got my methodology corrected, was forced into a real backtest, and ended at a very specific (and brutal) conclusion. I'm writing it up because the process shows what quant research actually looks like in practice, more clearly than any single backtest report could.

---

## Q1 (the opening): Backtests on a chaotic system — can they ever match live trading?

> "Backtesting is the standard validation tool in quant. But the stock market is a chaotic system — does a tiny change in initial conditions blow up into completely different outcomes after a while, like in real-world chaos? In other words, if a strategy is genuinely profitable, how do we know its backtest PnL and live PnL will agree over an equal-length window?"

**Answer**: The market isn't classical chaos. It's a **chaos + randomness + reflexivity + non-stationarity** hybrid.

Classical chaotic systems (Lorenz, double pendulum) have three properties: deterministic dynamics, sensitivity to initial conditions, bounded but non-periodic. The market differs:

1. **Not purely deterministic** — exogenous shocks (news, policy, earthquakes) and endogenous noise (order-flow microstructure)
2. **Reflexive** (Soros) — participants' expectations *change the system itself*. A double pendulum doesn't change because you observe it; an alpha disappears once enough people find it
3. **Non-stationary** — Lorenz parameters are constant, but market "parameters" (vol, correlation, liquidity) drift continuously

Three sources of initial-condition sensitivity in backtests: **micro-execution differences** (fill at 09:30:00 vs 09:30:03 → different share counts → cumulative path divergence); **path dependence** (+5% vs +3% on day 1 → different position sizing → different slippage → ...); **discrete jumps in the selected basket** (top-10 vs top-11 differ by 0.001 in score, but the held tickers can be entirely different).

**Key insight**: We don't aim for *path identity*; we aim for *distributional identity*. This is borrowed from chaos physics — **a single trajectory of the Lorenz attractor is unpredictable, but the shape of the attractor (the statistical properties) is stable**.

Operationally: read sharpe/drawdown/hit-rate as **distributions** rather than points; bootstrap-perturb the backtest and check the 5%-95% band; walk-forward + OOS; paper trading as a distributional sample; deflated Sharpe to correct for multiple testing.

**One sentence**: market paths must diverge, but a real strategy's statistical properties are an attractor that converges. **The craft of quant is replacing "betting on a trajectory" with "betting on the shape of an attractor".**

---

## Q2 (follow-up): Then why do most of my accounts beat the benchmark and look so promising?

My answer: this is exactly the form coincidence is most likely to take. The checklist:

1. **Beta ≠ Alpha** — β=1.2, market +15%, you +18% → looks like α=3%, actually α=0
2. **Factor premium** — 90% of Alpha158 factors are momentum/vol/volume — what you're earning is SMB/MOM/BAB, **known risk premia anyone can replicate**, not alpha
3. **Survivorship bias** — iShares IWB is *today's* R1000, the dead names from the past few years aren't in your pool
4. **Multiple testing** — across 20 accounts, P(≥12 beat) ≈ 25%; once you account for how many versions you tried, deflated Sharpe knocks "nice 2.0" down to ~0.5
5. **Short sample + bull bias** — high-β + high-momentum + small-cap-tilt strategies systematically beat in bulls and lose in bears; you've only seen one side of the coin
6. **Paper-trading hidden cheating** — zero market impact, zero latency, perfect liquidity, zero borrow cost, adjusted-close encoding future info

> **Real alpha usually doesn't *look* promising — sharpe 1.0–1.5, ugly drawdowns, the researcher themselves is uncertain. "Looks promising" in-sample is the most common signature of *no alpha*.**

---

## Q3 (user pushback): "QQQ as benchmark is already strict. Bull bias can't explain 7/10 beating. Why does yfinance adjust the data?"

Three real challenges. One by one:

**(a) QQQ as benchmark** — partly right: beating QQQ is harder than beating SPY. But still flawed: your universe is R1000 (1004 names, all sectors), benchmark is QQQ (100 names, tech-heavy) — universe mismatch. The right comparison is "5 names equal-weight randomly drawn from R1000". Otherwise your "excess" might just be a sector-allocation accident.

**(b) "Bull bias can't explain it"** — that's a misreading. Bull bias doesn't say "earning money in a bull = fake". It says: **high-β + high-momentum + small-cap-tilt strategies systematically beat in bulls and systematically lose in bears**. "Most of mine beat the market" is the textbook signature of that structure, not evidence of alpha.

**(c) Why adjusted prices?** — On a 4-for-1 split day, raw price drops from $200 to $50; without adjustment that day's return becomes -75%, totally wrong. So adjustment is necessary. But the cost: **adjusted close uses future information to retroactively rewrite history**. If your factor is "20-day MA", at time T you're seeing a "historical price at T-N" that's already been re-multiplied by a split factor only known at T+M — a mild **look-ahead bias**.

User: "Use those validation methods. Run the data."

---

## Methodology cheat sheet (for non-finance readers)

A pile of acronyms keeps showing up below — CAPM, 3-factor, β, α, Sharpe, MDD, R², Long/Short, t-stat. Let's flatten them all here so you don't have to bounce back and forth.

### What CAPM is, and what problem it solves

**The problem**: a strategy made +30% in a year. Is that the manager's skill, or did the market just rip +25% and drag him along? Absolute return alone can't tell you.

**CAPM** (Capital Asset Pricing Model) splits "return" into two pieces:

$$ R_{\text{strategy}} = α + β × R_{\text{market}} + \text{noise} $$

- **β (beta, exposure)**: how much you ride the market
  - β = 1 → market +1%, you +1% (you *are* the market)
  - β = 2 → market +1%, you +2% (2× leveraged long)
  - β = 0 → market is irrelevant to you (market neutral)
  - β = -1 → market +1%, you -1% (net short)
- **α (alpha, excess return)**: what's left after subtracting β × market. **This is the actual skill** — what you earn regardless of which way the market moves.
- **Noise**: the residual you can't explain.

Example: market +25%, you +30%, your β regresses out as 1.2. Then expected = 1.2 × 25% = 30% → **your α = 0**. You just dialed up market exposure 1.2× — no skill.

> **One line**: CAPM separates "what the market handed you" from "what you actually did" — the cure for "looks promising but it's just naked long".

### 3-factor attribution (Fama-French)

**The problem**: CAPM only strips out one free lunch (the market). But academia found **several more long-term free lunches** anyone can eat:

- **Small caps beat large caps long-term** (SMB = Small Minus Big)
- **Value beats growth** (HML = High Book-to-Market Minus Low)
- **Past winners keep winning short-term** (MOM = momentum)

These are **factor premia** — buy the matching ETF and you collect them, no skill required. So "beating SPY" isn't enough; you need to **strip all the known factors** and see if anything's left.

3-factor extends CAPM:

$$ R_{\text{strategy}} = α + β_{\text{mkt}} R_{\text{mkt}} + β_{\text{size}} R_{\text{SMB}} + β_{\text{mom}} R_{\text{MOM}} + \text{noise} $$

The β_QQQ / β_size / β_mom in our tables are these three regression coefficients. We use **IWM-QQQ as SMB proxy** (small minus large ETF spread) and **MTUM-QQQ as MOM proxy**.

> **One line**: 3-factor attribution is CAPM upgraded — strip market + size premium + momentum premium **all three free lunches** and only count the residual α as real skill.

### How do we know α is real and not noise — t-stat

The α number alone isn't enough. A strategy can post α = 5% from real skill or from 14 days of luck. The discriminator is the **t-statistic**:

$$ t = \frac{\text{estimate}}{\text{standard error}} = \frac{\hat α}{SE(\hat α)} $$

Rules of thumb:
- **|t| > 2 ≈ 95% confidence** — barely "statistically significant"
- **|t| > 3** ≈ minimum bar for shipping a product in industry
- **|t| < 1** ≈ almost certainly noise

The "α t-stat = 0.90 / -3.41" you'll see in the tables is exactly this. A 💀 in a table means |t| > 2 **and** the sign is *negative* — statistically significant *negative* alpha.

### Sharpe Ratio

$$ \text{Sharpe} = \frac{\text{annualized return}}{\text{annualized volatility}} $$

"How much return per unit of risk." Rough scale:
- < 0: bleeding
- 0–1: marginal
- 1–2: good
- 2–3: very good (top quant fund territory)
- **>3 sustained**: extremely rare — **or your data has a bug**. That's why a backtest printing Sharpe 2.4 immediately puts me on alert.

### MDD (Maximum Drawdown)

The biggest peak-to-trough decline in the strategy's history. MDD = -50% means the account once dropped from $1M to $500k. **Measures pain tolerance + leverage blow-up risk.** Drawdowns over -80% basically equal "technical bankruptcy" — very hard to recover from.

### R² (R-squared, coefficient of determination)

The "fit quality" of a regression, between 0 and 1. R² = 0.5 means the factor model explains 50% of strategy returns. R² too high (>0.8) means the strategy is essentially a copy of the factors; R² too low (<0.05) combined with β ≈ 0 typically means **the regression is broken** — the 1-year backtest's β=0 / R²<2% in this article is a textbook example of that bug.

### Long / Short / Long-Short

- **Long**: buy, betting it goes up. All A-group accounts are long-only.
- **Short**: borrow shares, sell them, then buy them back later cheaper to return — profit on the drop.
- **Long-Short**: simultaneously long one basket + short another. Standard recipe is **long top-5 / short bottom-5** — equal capital each side, market direction cancels out, residual PnL is just "top vs bottom relative spread", **un-contaminated by market β**. The gold standard for "does the ranking actually contain information?".

### IC (Information Coefficient)

Rank all stocks by your factor score, then check next-day actual returns ranking — Spearman rank correlation between the two. Range -1 to +1:
- IC > 0: high-scored names rise more → signal works
- IC = 0: unrelated → noise
- IC < 0: high-scored names *fall* more → **reverse signal**

Academic rule: single-factor **|IC mean| ≈ 0.02–0.05** already counts as decent alpha; **|IC t-stat| > 2** is needed to reject "this is random".

### β / size / momentum tilts

- **High-β tilt**: portfolio β > 1, essentially "leveraged long the market"
- **Size tilt**: portfolio systematically tilts to small caps, eating SMB premium
- **Momentum tilt**: portfolio systematically tilts to past winners, eating MOM premium

Each of these is a factor premium, **not alpha** — they make you look profitable, but anyone buying the corresponding ETF (IWM, MTUM) replicates them.

---

## Experiment 1 (14-day paper): a pretty but fragile report

Ran CAPM + 3-factor attribution. **Findings, very informative and counter to the gut feel**:

- **Beat QQQ: 9/59 = 15%** (looks shocking)
- A-group **β ≈ 2.0** — the "nice returns" are just 2× leverage
- β_mom +1.5 to +2.5 — heavy momentum-factor exposure
- **No account has |t(α)| > 2** (highest A10 = 0.90)
- IDX1 (buy-and-hold) has higher α t-stat than most strategies

---

## User pushback 1: "Only 4/10 A-accounts trail QQQ — why are you saying 15%?"

**A genuine methodological mistake.** I'd put all 59 US accounts in the denominator, but the CN accounts returned 0% cumulative due to a timezone bug — they got counted as "trailing QQQ". The denominator was contaminated.

Corrected breakdown: A-group **7/10 beat QQQ = 70%**, B-group 1/10, IDX 1/2. I apologized. But **the core conclusion holds** — binomial test (n=10, p=0.5, k≥7) gives p=0.17, not significant; A-group α t-stat all < 1 in CAPM, the 70% hit rate is fully explained by β≈2.

User then asked: "What if we backtest A-group strategies for a full year — does the noise drop?"

I said yes, but watch four pitfalls: ① IS overfit (turns out doesn't apply here — see below) ② look-ahead in long samples (adjusted close, point-in-time universe) ③ regime monoculture (must cover bear/rebound/bull) ④ long sample still requires factor attribution.

---

## User pushback 2: "A-group just randomly picks 10 fixed Alpha158 factors. There's no training. What 'walk-forward' are you talking about?"

**Hit the bullseye, I was wrong again.** Two kinds of look-ahead must be distinguished:

| Type | Source | Does A-group have it? |
|---|---|---|
| Training leakage | Future data used to fit parameters | ❌ No (no training) |
| Data leakage | Factor calc uses info "not yet known on that day" | ⚠️ Yes (adjusted close + universe) |

Walk-forward / OOS **don't apply to A-group** — no training set means no train-test leakage. I apologized again.

User: **"Run the actual production config, backtest one year, evaluate."**

---

## Experiment 2 (1-year backtest): the dramatic "+245% / sharpe 2.4"

A01–A10 actual production config (5 factors equal-weighted, top_5 hold, daily rebalance), 2025-05-15 → 2026-05-04, 989 R1000 names, T+1 open buy / T+2 open sell (avoiding close-execution look-ahead).

| Strategy | Cumulative | Sharpe | MDD |
|---|---|---|---|
| A04 trend | **+244.6%** | 2.40 | -25.6% |
| A08 value+mom | **+190.9%** | 2.52 | -27.3% |
| A01 momentum | **+171.0%** | 2.40 | -33.8% |
| A10 adaptive | +113.5% | 2.04 | -18.6% |
| ... | | | |
| A02 mean-rev | -27.8% | -1.15 | -39.9% |
| A09 reversal | -34.6% | -1.32 | -48.4% |

**The numbers themselves are a red flag.** Top quant funds (Renaissance, Two Sigma) run long-term sharpe 2–3 / annualized 30–50%. A **no-training, fixed-formula, randomly-picked-5-factors** strategy hitting sharpe 2.5 / +200% annualized — **physically implausible**.

Also CAPM showed β ≈ 0, R² < 2% across the board — a long-only 5-stock book with β=0 violates basic finance. That's a **timing-misalignment artifact**: positions are open[t+1]→open[t+2] but the benchmark is close[t-1]→close[t]; the one-day shift collapses the regression and inflates α.

I said: "To trust this, we need to fix timing alignment, add slippage, split by regime, and address survivorship."

User: "OK, keep fixing."

---

## Experiment 3 (fixed 4-year backtest): everything collapses, and most go negative

Three fixes: ① benchmark also open-to-open (kill the timing artifact) ② 10bp/side slippage ③ window stretched to 2022-04 → 2026-05 (2022 bear + 2023 rebound + 2024–25 bull).

| Strategy | Pre-fix 1y | Post-fix 4y cumulative | 4y Sharpe |
|---|---|---|---|
| A01 momentum | **+171%** ✨ | **-3.9%** | 0.22 |
| A04 trend | **+245%** ✨ | **-51.6%** | 0.06 |
| A08 value+mom | **+191%** ✨ | **-12.4%** | 0.19 |
| A10 adaptive | +113% | +11.5% | 0.29 |
| A06 composite | +53% | **-81.5%** 💀 | -0.76 |
| A02 mean-rev | -28% | **-87.2%** 💀 | -1.53 |
| A03 vol-price | +1% | **-78.5%** 💀 | -0.86 |
| A07 short-mom | +6% | **-79.4%** 💀 | -1.03 |
| A09 reversal | -35% | **-87.1%** 💀 | -1.37 |

**10/10 strategies trail QQQ (+32%). 5 of them lost 80%+, technically bankrupt.**

CAPM exposed the truth about β: post-fix β goes from a fake 0 to a real 0.65–1.49 — every strategy is **structurally long the market**. A02/A03/A07/A09 have α t-stat < -2.5, **statistically significantly negative** — they actively destroy value.

3-factor attribution shows β_size ≈ +0.8 across the board — strategies are **heavily exposed to small-cap risk**. Mechanism: Alpha158's ROC/MA_RATIO/STD-class factors swing more wildly on small caps with more extreme rankings, so a top_5 extremes-portfolio auto-selects small caps. Not alpha — **structural size tilt**.

The regime table delivers the killing blow: **no strategy makes money across all regimes**. The best-looking A08 made +66% in 2024 but -32% in 2022 / -17% in 2023 — textbook "lucky year" pattern. A10 +40% in 2026 YTD looks great — but 2025 it was -41%.

---

## Final answer — back to the original question

> "Why do most accounts beat the benchmark and look so promising?"

**Because you only saw a 14-day snapshot, and that window happened to be the peak of an April-2026 momentum rally.**

The full picture:

1. 4-year cumulative: **10/10 trail QQQ**
2. 4-year risk-adjusted: **10/10 α ≤ 0**, 4 of them statistically significantly negative
3. β = 1.0 + size tilt = +0.8 → what you really hold is **levered small-cap exposure**
4. The 14-day "7/10 beat" is just incidental sampling of a high-momentum month in 2026Q2

A-group is **not "a neutral system that hasn't found alpha yet"**. It's:

> **A levered multi-factor beta + structural size tilt + high-turnover-friction *negative-alpha* system.**

The daily turnover bleeds ~10bp × high turnover ≈ -25% annualized. That's why "buy SPY and sit" beat 10 active strategies.

---

## Follow-up: "is the signal eaten by friction, or is the signal itself reversed?"

The story isn't actually over. Every conclusion above is shadowed by two confounders — **β exposure** and **transaction friction**. So the "10/10 wipeout" admits two very different explanations:

- **Hypothesis A**: the strategy did pick the right stocks, but **slippage + high turnover + leverage** ate the signal. If true, **fixing the trading layer (lower frequency, ETF wrappers) saves it**.
- **Hypothesis B**: the strategy picks the *wrong* stocks — **Alpha158's ranking has no information in our universe, possibly even reversed**. If true, no amount of trading-layer optimization helps; the signal source itself is broken.

Two tools separate these:

### Tool 1: Pure IC (separate "can it pick stocks" from "can it hold them")

**IC = Information Coefficient** — formal-sounding but plainly stated:

> Rank all today's stocks by your factor score → check whether tomorrow's actual return ranking **lines up with your ranking**.

Mathematically it's the **Spearman rank correlation** between two rankings, in [-1, +1]:

- IC = +1 → your highest-scored stock returns the most tomorrow, lowest returns the least. Perfect prediction.
- IC = 0 → your ranking has **no relationship** to tomorrow's returns. Pure noise.
- IC = -1 → what you thought was best does worst. Perfect **reverse** prediction.

Academic rule of thumb: a single-factor IC averaging **0.02–0.05 already counts as meaningful alpha** (looks tiny, but compounding daily can yield 10%+ annualized). What we look at isn't absolute size but whether the **mean IC is significantly different from 0** — i.e. **IC's t-stat**.

The point of IC is it has **zero trading machinery in it**: no positions, no turnover, no slippage, no β. It just asks: "does your factor score have anything to do with future returns?" If even IC is zero, no "friction ate the alpha" defense holds — **you never picked the right stocks to begin with**.

### Tool 2: Long-Short neutralization (kill market β)

All earlier backtests were long-only, riding the market. **Long-Short** is the industry standard:

- **Simultaneously** long the top-5 ranked names and short the bottom-5
- Equal capital each side
- The market's up/down cancels out → β ≈ 0
- What's left in the PnL is **purely the ranking's alpha**

If long-short still earns over 4 years → the ranking has signal, the failure was β/size/friction. If long-short loses too → the ranking is wrong.

---

### Results (4 years, 2022-04 → 2026-05)

| Strategy | IC mean | IC t-stat | Long-Short cum | L-S Sharpe | Long-only cum | Short-only cum |
|---|---|---|---|---|---|---|
| A01 | -0.0063 | -1.17 | **-73.5%** | -0.78 | -4.0% | **-96.9%** |
| A02 (rev) | +0.0058 | +1.20 | **-88.3%** | **-3.09** 💀 | -87.2% | -91.4% |
| A03 | -0.0043 | -1.19 | -85.6% | -2.34 | -78.5% | -93.2% |
| A04 | -0.0062 | -1.15 | -84.7% | -0.81 | -51.6% | -98.7% |
| A05 | -0.0026 | -0.46 | -61.3% | -0.86 | -24.0% | -86.0% |
| A07 | -0.0069 | -1.45 | **-91.2%** | **-3.41** 💀 | -79.4% | -97.3% |
| A08 | -0.0056 | -1.22 | -78.8% | -1.28 | -12.4% | -97.0% |
| A09 (rev) | +0.0052 | +1.18 | -85.8% | -2.59 | -87.1% | -88.1% |
| A10 | -0.0031 | -0.65 | -64.2% | -0.97 | +11.5% | -91.7% |

**Three chained explosions:**

**(1) ICs are all near zero — and systematically biased negative**
8/10 strategies have negative mean IC (A02/A09 are mean-reversion strategies that flip the rank, so their IC looks +0.005, but it's the same thing reversed). **No strategy has |IC t-stat| > 2** — pure-statistically, "ranking by Alpha158 score" provides information **no better than random**.

But every number is **slightly tilted backwards**. That's not a coincidence. It says: high-scored stocks return slightly less the next day, low-scored slightly more. We thought it was "friction ate the alpha"; **actually the ranking is working in reverse**.

**(2) Long-Short is even more catastrophic**
After β is killed, 10/10 strategies lose 4-year long-short, 9/10 lose >60%. A07's long-short Sharpe is **-3.41** — after multiple-testing correction this is **statistically significantly negative alpha**.

This kills the "eaten by friction" hypothesis — long-short has the same turnover as long-only, but **after long-short hedges the market away, all that's left is the ranking's pure signal — and that signal points the wrong way**.

**(3) Short-only mostly -86% to -99%**
The short side is the smoking gun. If the ranking had no information, shorting top-5 should look similar to longing bottom-5. But **shorting "the top-5 highest-scored names" loses 90%+ across the board**.

Why? Shorting top equals shorting "high-score + actually-rallied" stocks (the market leaders in a bull). That's **the reverse alpha amplified on the short side** — you didn't just miss, you precisely picked the wrong direction.

---

### Cross-market verification: per-factor decomposition + porting to CN A-shares

So *why* is it reversed? Is it Alpha158's formulas, or the R1000 universe? Need **per-factor decomposition** + **cross-market test**.

Take each factor on its own, compute daily cross-sectional IC:

**US R1000 (32 factors)**:

| Rank | Factor (most negative IC) | IC mean | IC t-stat |
|---|---|---|---|
| 1 | BBPOS_10 (Bollinger Position 10d) | -0.0076 | **-1.65** |
| 2 | BBPOS_5 | -0.0073 | -1.63 |
| 3 | ROC_5 (5-day momentum) | -0.0082 | -1.55 |
| 4 | MA_RATIO_5 | -0.0079 | -1.51 |
| 5 | BETA_5 | -0.0067 | -1.28 |

**0 factors with |t| > 2 in US.** All "momentum / short-term MA deviation / Bollinger position" factors are mildly reversed, but **strength insufficient to reject random**.

**CN CSI300 (same 32 factors)**:

| Rank | Factor (most negative IC) | IC mean | IC t-stat |
|---|---|---|---|
| 1 | KSFT (K-line shift) | **-0.0288** | **-4.64** 💥 |
| 2 | KMID (K-line body) | -0.0269 | **-4.50** 💥 |
| 3 | STD_10 | -0.0296 | **-4.19** 💥 |
| 4 | KLOW | -0.0195 | **-3.98** |
| 5 | KLEN | -0.0248 | -3.74 |

**15 / 32 factors |t| > 2 in CN. All negative IC** — zero positive-significant.

**Cross-market consistency**: 26/32 factors share the **same sign** in both markets (81%). Zero factors are **simultaneously positive and significant** in both — i.e., **Alpha158 has no robust positive alpha factor in either market**. But the reverse tendency is **consistently present** and strong enough in CN to reject the random hypothesis.

---

### Final revised conclusion

Stitching the two experiments back, A-group's portrait sharpens from "levered negative-alpha system" to a much more specific statement:

> **Alpha158's ranking signal in R1000 + CSI300 is systematically biased toward reversal. In US large caps the reversal effect is weak (largely arbitraged away), so it doesn't reach significance; in CN A-shares it's strong, with IR ≈ 1.7–2.3.**
>
> **A-group has been treating a *reverse* signal as a forward signal from day one — it didn't fail to find alpha, it found *anti*-alpha.**

Because that's counterintuitive, let me say it again in plain language:

- Imagine a lottery: pick 5 cards from 1004, scored by some formula.
- If the formula is useless → expected return = market return (β=1, you get whatever the market gives)
- If the formula has positive alpha → expected return > market (you beat it)
- If the formula has **negative alpha** → expected return < market (the market eats you)

A-group is option 3. Every day it picks stocks by a **reverse-effective** formula — longing what should be shorted, shorting what should be longed. In a rising market, "going the wrong way" still claws back some losses (because the bad stocks you longed also went up) — that's why over 4 years it "only" lost 50–80% rather than something worse.

**The "friction ate the alpha" hypothesis is empirically rejected.** Lower frequency, different broker, ETF wrappers — none of these save A-group. The only meaningful next steps are (a) **flip every Alpha158 factor's sign**, or (b) replace the signal source entirely (Q-group's Qlib models learn the signs and weights from data, instead of trusting Alpha158's hand-coded directional assumptions).

A deeper lesson: **manually setting factor direction ("I think momentum should be positive") can systematically break in a reversal market**. Alpha158 is the factor library Microsoft Qlib used in its early days; defaults like "higher ROC/MA_RATIO/BBPOS = bullish" might have held in 2010–2015 US, but in 2022–2026 R1000/CSI300 they're clearly inverted. That's why Q-group (let the model learn weights and signs) is worth pushing forward.

---

## What this back-and-forth taught

Looking back, **almost every key turn was driven by the user's pushback**:

1. **Opening with the chaos question** — getting "path vs distribution" straight first is what lets the later attribution mean anything
2. **"My accounts look promising"** — forced out the full checklist of beta / factor premium / survivorship / multiple testing
3. **"How did you get 15%?"** — surfaced a CN-timezone-contaminated denominator bug
4. **"A-group has no training, what walk-forward?"** — corrected my misuse of walk-forward
5. **"Just backtest a year"** — pushed verbal discussion into a falsifiable experiment
6. **"Keep fixing"** — turned a "sharpe 2.4 victory" into "total wipeout"
7. **"Eaten by friction, or signal itself reversed?"** — forced the IC + Long-Short + cross-market triple, sharpening "negative-alpha system" into **a reverse-effective ranking signal**

Every pushback was doing the same thing: **refusing to let pretty numbers stand as conclusions**. That's the actual craft of quant research — not producing +245%, but knowing +245% is almost certainly a bug, and knowing which confounder to peel off at each step.

Two **known but un-fixed** biases remain (survivorship + adjusted-close look-ahead). Patching them won't change the direction; it will only make things worse.

If we want one positive takeaway from this negative result: **A-group's purpose was never to make money — it was to map out what is *not* alpha, and along the way we mapped out what *is* anti-alpha**. Mission accomplished on that.


