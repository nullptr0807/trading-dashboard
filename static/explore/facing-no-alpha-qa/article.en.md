# After Four Attribution Articles All Said "No Alpha": An Honest Q&A

**Date**: 2026-05-05
**Category**: Methodology / Concepts

## Setup

Read the four explore articles in sequence and you arrive at a fairly bleak picture:

- **A-group** (Alpha158 multi-factor): 80 CAPM+FF3 regressions, **0 with |t(α)| > 2**. The pretty YTD curves were almost entirely SMB (small-cap) style exposure.
- **B-group** (gplearn-mined GP factors): 8 quarters of walk-forward, 24 regressions, **0/24 significant α**, max t = 1.96. GP systematically learned an "anti-small-cap + anti-momentum" style on CN data — a **risk factor**, not an alpha signal.
- **Composite-method comparison**: V0/V1/V2 — all three had near-zero IC on R1000. The "30%+ annualized" pieces were almost pure style beta.
- **Position-sizing experiment**: industry-standard tools like InvVol and VolTargeting **hurt** Sharpe when stacked on a weak signal.

So the natural question is: **if there's no alpha anywhere, what next?**

This article is not another experiment. It's a writeup of the seven concept questions that came up repeatedly in the last conversation — because those questions define the feasibility boundary of any "improvement plan."

---

## Q1. What is t-stat, and why is the threshold 2?

t-statistic = estimate / standard error. In an attribution regression,

$$ t(\alpha) = \frac{\hat\alpha}{\text{SE}(\hat\alpha)} $$

Rule of thumb: **|t| > 2 ≈ 95% confidence (two-tailed)**. In finance this is just the *minimum* visibility threshold; production-grade alpha usually requires **|t| > 3**.

The most painful formula — it directly tells you how long a sample needs to be to *possibly* show significant alpha:

$$ t(\alpha) \approx \text{IR} \times \sqrt{T} = \frac{\alpha_{\text{ann}}}{\sigma_{\text{ann}}} \times \sqrt{\frac{n_{\text{days}}}{252}} $$

Plug in numbers. Suppose you **actually have** α = 6% and σ = 15% (IR = 0.4, already strong):

| Sample | t | Significant? |
|---|---|---|
| 1 year | 0.40 | ❌ |
| 2 years | 0.57 | ❌ |
| 5 years | 0.89 | ❌ |
| **10 years** | **1.26** | still no |
| 25 years | 2.0 | borderline |

In other words — **even with real alpha, a 2-year sample can never produce |t| > 2**. The fact that our attribution wasn't significant is **partly insufficient sample, not necessarily a dead strategy**. Good news and bad news.

---

## Q2. Can 5-minute bars expand the sample?

**No.** Classic temptation trap.

Going from daily to 5m bars: 78 bars/day, ~39000 samples over 2 years vs ~500 daily — looks like √T should grow ~8.8×. But:

1. **IR is invariant to resampling.** Annualized α and σ both compound by calendar time. t depends only on **calendar time and signal-to-noise quality**, not granularity.
2. **Microstructure noise explodes.** Bid-ask bounce, tick-level heteroskedasticity, autocorrelation — i.i.d. residual assumption breaks, t-stat becomes **artificially inflated** (false significance).
3. **Trading costs.** Daily turnover ~30%; 5-minute easily 10× more, all slippage/fees scaled accordingly.
4. **You're forced into a different game** — HFT plays order books and adversary observation, not statistical price factors (see Q5).

Conclusion: **to expand sample, you either wait, or do cross-market OOS.** Resampling is not the answer.

---

## Q3. What about a 7-8 year backtest? Doesn't alpha-decay say half-life is 2-3 years?

A paradox I sat with for a while:

- To prove |t| > 2 you need 5+ years;
- but alpha half-life is 2-3 years, so anything that worked 5 years ago is probably dead today.

**Strictly, you can't have both.** The industry answer is not "rigorous proof" but *preponderance of evidence* — a coalition of signals:

1. **Long samples** (5-10y) calibrate **risk models and β** — risk factors are extremely stable, much longer half-lives.
2. **Short samples** (recent 1-2y IS + 3-6mo OOS) **monitor whether alpha is still alive**.
3. **Cross-validation**: explainable economic mechanism + cross-market OOS + cross-regime (bull/bear, rate cycles) + paper-trade OOS.
4. **Retirement protocol**: rolling IC turning negative or t-stat staying below threshold → take it offline. **Accept decay, don't fight it.**

Long backtests aren't there to "prove alpha is real." They're there to **prove your β / risk model is solid, and that the current alpha was at least alive in some past window**. Whether it's alive *tomorrow* is a question only tomorrow can answer.

---

## Q4. Is HFT the same thing as daily-frequency factors?

**No.** Rough comparison:

| Dimension | Daily factors (A/B) | HFT |
|---|---|---|
| Data | OHLCV, ~20 columns | full order book, tick, messages |
| Signal nature | **statistical proxies** (price/vol = shadows of behavior) | **direct adversary observation** |
| Half-life | months–years | ms–minutes |
| Capacity | medium–large | small (self-impact) |
| Infra | Python + SQLite suffices | colocation, FPGA, kernel bypass |
| Significance source | √T (years) | √N (millions of trades/day) |

HFT achieves giant t-stats not because "high frequency" — but because it **samples millions of microstructure events per day**, while daily factors sample one "market state" per day.

---

## Q5. Does cross-market OOS count as "proof"?

Strong evidence, but still not strict proof.

What strong evidence looks like:

1. **Transferable economic mechanism** — e.g., reversal as "compensation to liquidity providers" works in any market with liquidity demand.
2. **Independently significant on N independent markets** (US + CN + EU + JP) — each market is one independent experiment. Combined p-value drops fast.
3. **Across regimes** — survives rate hikes/cuts, bull/bear.
4. **Paper-trade OOS** holds for 6-12 months without decay.

Our current assets: US (R1000) + CN (CSI300). For a research program aiming to "prove" alpha, this is the **minimum**, not a luxury.

---

## Q6. How do you tell whether a complex factor has "economic intuition"?

GP often spits out things like `sign(ts_argmax(divide(close, open), 5)) * delta(volume, 3)`. How do you tell if it has a story? Four angles:

1. **Project onto Barra style factors**: regress the factor on Size/Value/Mom/Vol etc.; see how much IC remains in the residual. If IC ≈ 0 after, it's a known style in disguise.
2. **Eyeball extreme samples**: pull the top/bottom 20 names by factor score and look — do they share interpretable features (sector, cap, news)?
3. **Random-label control**: run the same GP pipeline on shuffled labels — if the resulting "factors" look statistically similar to the real ones, you've been overfitting.
4. **Write a "narrow and specific" story**: a good factor should reduce to "company X in regime Y is mispriced by investor type Z." If the only story is "top quintile beats bottom quintile," that's a tautology, not a story.

---

## Q7. Are FF3 and IC the same thing? Why look at both?

**No. Different angles.**

- **Fama-French 3-factor (FF3)** is **return decomposition**: `r = α + β_Mkt·MKT + β_SMB·SMB + β_HML·HML + ε`. Tells you how much of your P&L is market, size, value, vs. real edge. **Unit: dollars.**
- **IC (Information Coefficient)** is **cross-sectional predictive power**: per cross-section (per day/week), Spearman correlation between factor value and next-period return; then time-series of those. **Unit: correlation.**

Empirical bands:

| IC (cross-sectional Spearman) | Verdict |
|---|---|
| 0.00–0.01 | noise |
| 0.02–0.03 | weak signal |
| 0.03–0.05 | good factor |
| 0.05+ | rare |
| ICIR > 0.1 | stable |

**Why both**: FF3 tells you "is the money I made actually alpha money"; IC tells you "does the signal still have predictive power right now." A factor can have high IC but insignificant FF3-α (style explains it away), or weak IC but occasional significant α (luck). Only when both agree is it real.

---

## So what's the improvement path?

By ROI, the next things worth doing (carried over from the last conversation):

**P0 (this month)**
- **A/B/AB-composite multi-account aggregation backtest**: σ↓ → t-stat × 2-3. Free leverage, no new research required.
- **Run the Q-group through the full walk-forward + FF3 + V2 IC pipeline** — only Q-group hasn't been attributed.

**P1 (next month)**
- Replace vol-targeting with **drawdown stop** (the sizing experiment showed vol-target hurts Sharpe).
- **Cross-market OOS**: run US-validated factors on CN, vice versa. Free independent validation.

**P2 (quarterly)**
- Expand the **factor library** to fundamentals, alternative data, cross-sectional time-series datasets. Stop polishing composites — composition isn't the bottleneck.
- Reposition **B-group's negative β** as a "portfolio diversifier" product, not an alpha product. Even without significant alpha, negative β has portfolio-level value.

---

## TL;DR

The "no alpha" verdict from four attribution articles is **not an endpoint — it's a calibration**. Calibration of our real understanding of sample size, t-stats, alpha decay, and risk factors. Honestly admitting these bounds is more valuable than pretending alpha exists. The next phase isn't "mine one more magic factor"; it's **make the risk model trustworthy, push t > 2 via multi-account aggregation, treat cross-market OOS as independent evidence**.

Unromantic. But it works.
