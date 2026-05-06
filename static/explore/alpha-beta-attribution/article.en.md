# α or β? An Honest Attribution of A-Group's 20 Paper Accounts

> Date: 2026-05-04  ·  Author: Cyber Quant Arena  ·  Category: Performance Attribution

## 1. The Open Question from Last Post

The [previous article](./factor-composite-normalization) found A-group running V1 composite produced 37% YTD annualized return with Sharpe 2.13. We claimed "this is mostly style beta, not alpha" — but that was an indirect inference from V1 vs V2 comparison.

This post does the direct decomposition. For each account, multi-factor attribution tells us how much return came from:
- riding the market (CAPM β)
- exposure to small-caps (SMB β)
- exposure to momentum (MOM β)
- residual α (genuine stock-picking skill)
- and whether that α is **statistically significant** (t-stat > 2)

Four panels: US/CN × 2y/YTD, 10 A-group accounts each.

> ⚠️ Q-group (Qlib models) skipped — needs walk-forward retraining (~40 hrs single-machine). B-group (GP) deferred to a follow-up post. This article covers A-group only — fully deterministic formulas, zero-cost 2-year replay, no look-ahead.

---

## 2. Methodology

### 2.1 Offline replay
Each A-group strategy (A01–A10) is fully specified by `factors/signal.py` V1 logic + `accounts/strategies.py` config (`factor_names`, `top_n`, `strategy_type`). Pure deterministic formulas, replayed on 2 years of price data — no look-ahead, no surviving training-set noise.

### 2.2 Attribution models

**CAPM single-factor**:
$$r_a = \alpha + \beta \cdot r_{mkt} + \varepsilon$$

**Fama-French 3-factor (simplified)**:
$$r_a = \alpha + \beta_{mkt} r_{mkt} + \beta_{smb} r_{smb} + \beta_{mom} r_{mom} + \varepsilon$$

Where:
- US: r_mkt = SPY, CN: r_mkt = CSI 300 (000300.SH)
- r_smb = bottom-30% liquidity portfolio − top-30% (using avg dollar volume as size proxy)
- r_mom = top-30% trailing-12m return − bottom-30%

### 2.3 Significance threshold
**|t-stat| > 2** is the bar to call a coefficient "non-zero". t < 2 means we lack evidence to reject "α = 0" no matter how big the point estimate looks.

---

## 3. CAPM Results (4 Matrices)

### 3.1 Scatter plots (α annualized %, vs β)

Red = |t(α)|>2 significant; blue = not significant.

**US 2y**:
![scatter_US_2y](scatter_US_2y.png)

**US YTD**:
![scatter_US_YTD](scatter_US_YTD.png)

**CN 2y**:
![scatter_CN_2y](scatter_CN_2y.png)

**CN YTD**:
![scatter_CN_YTD](scatter_CN_YTD.png)

### 3.2 Key Observations

#### Observation 1: Zero significant α under CAPM
4 matrices × 10 accounts = 40 regressions, **0 with |t(α)| > 2**. The highest is US YTD A09 at -1.94 (and α is *negative*). **No account can statistically prove it has alpha at the CAPM level.**

The flashy US YTD: A01 (α=147% ann), A04 (α=117% ann), A10 (α=127% ann) — all t(α) between 1.5–1.7, "trending but not significant".

#### Observation 2: β is small (|β| < 0.7) almost everywhere
A-group accounts have very low correlation with their benchmark; R² mostly < 0.07. Cause: 3–8 holdings per day means high concentration, dominated by single-stock idiosyncratic noise. Not inherently good or bad — but it means **these aren't "market index + leverage"** dressed up as strategy.

#### Observation 3: CN accounts have systematically higher β than US
CN 2y avg β ≈ 0.4; US 2y avg β ≈ -0.13. CN follows CSI 300 closely; US is nearly independent of SPY. Likely cause: CN universe is only 300 names, A-group picks 5/day → high index-component overlap. US universe is 1000 names → low overlap.

#### Observation 4: US 2y β is negative, US YTD β flips positive
Look at A03, A05, A07: 2y β = -0.31, -0.32, -0.24, **inverted**. YTD they all flip positive. Strategy has a regime shift; **the long-run β ≈ 0 isn't because it's "market-neutral" — β itself is volatile**.

---

## 4. FF3 Results (adding SMB + MOM)

### 4.1 Highlights (filtering |t|>1.5)

Full table in `summary_ff3.csv` (linked at the end). Key findings:

**US YTD — SMB exposure is the story**:

| Account | t(α) | t(SMB) | t(MOM) | R² | Reading |
|---|---|---|---|---|---|
| A01 | 1.36 | **2.22** | 0.44 | 0.10 | Returns mostly small-cap exposure |
| A04 | 1.50 | **2.82** | -0.40 | 0.15 | Returns mostly small-cap exposure |
| A05 | 0.21 | **2.47** | -1.42 | 0.12 | Pure small-cap beta |
| A07 | -0.29 | **2.02** | 1.34 | 0.06 | α explained as style |
| A06 | 0.17 | 1.00 | -0.01 | 0.08 | Significant market β=0.79 |

**This is the smoking gun**: last article's hypothesis — "A-group's YTD returns are mostly style beta" — is **directly confirmed by the FF3 model**. A04's SMB β=1.69, t=2.82 means each day's return roughly 1.7× tracks a small-cap portfolio.

**CN highlights**:
- CN A03 YTD: t(MOM) = **2.16**, α t = -1.81. Significantly *dragged* by momentum — negative contribution.
- CN A09 2y: t(SMB) = -2.03, t(MOM) = -2.17. "Inverse" exposure to both.

### 4.2 Any α survive after adding SMB+MOM?

**No.** 40 FF3 regressions, **none with |t(α)| > 2**.

Top t(α) candidates:
- US A04 2y: t=1.95 (edge)
- US A01 2y: t=1.84
- US A10 2y: t=1.89
- US A10 YTD: t=1.78

All "trending but cannot reject α=0".

---

## 5. Equity Curves

Each panel overlays all 10 A-group accounts vs benchmark:

**US 2y** (vs SPY, dashed black):
![equity_US_2y](equity_US_2y.png)

**US YTD**:
![equity_US_YTD](equity_US_YTD.png)

**CN 2y** (vs CSI 300):
![equity_CN_2y](equity_CN_2y.png)

**CN YTD**:
![equity_CN_YTD](equity_CN_YTD.png)

Visual takeaways:
- US 2y: high dispersion, accounts cross over each other, none clearly beats/lags SPY.
- US YTD: several accounts beat SPY, A09 lags badly — looks like luck, not skill.
- CN: paths track CSI 300 direction, less dispersion than US (consistent with higher β).

---

## 6. Conclusion: Which Accounts Have Real Alpha?

**None. Zero.**

40 CAPM + 40 FF3 = 80 regressions, **not a single |t(α)| > 2**.

This doesn't say A-group is losing money (some accounts have decent absolute returns). It says:

> **Their P&L cannot be statistically distinguished from (a) luck (b) style exposure (c) both, rather than from stock-picking skill.**

This is a direct numerical confirmation of last article's V2 experiment finding: after stripping size/sector/momentum styles, IC collapses to zero — equivalently, α doesn't survive significance tests.

---

## 7. Why t-stat ≥ 2 Is So Hard

A-group account daily vol annualizes to ~30–55%. To prove a real α=15%/year at t>2, sample size needed:

$$n \gtrsim \left(\frac{2 \cdot \sigma_{ann}}{\alpha_{ann}}\right)^2 \times 252 \approx 1700 \text{ days} \approx 7 \text{ years}$$

**We have 2 years (330–482 days) and 4 months (76–81 days)**. Even if A-group genuinely has 15% alpha, it would take 5 more years to prove it statistically.

A useful rule to remember: **"is this α real?"** and **"how long until we can prove it?"** are independent questions. The latter is determined by α magnitude, volatility, and sample size. In small-account, short-horizon, high-vol regimes, **most "looks-like-alpha" can't survive a 95% confidence test**.

---

## 8. So What Should We Do?

### 8.1 Don't make strategic decisions on 4 months of data
The flashy US YTD names (A01/A04/A10) all have t(α) < 2. Their SMB β is high — **a small-cap regime flip will rapidly give it back**. Treating that paper P&L as "good strategy" is a category error.

### 8.2 Use FF3 to monitor per-account exposure
Exposure isn't bad — many funds intentionally take style exposure to harvest risk premium. But **you must know what you're harvesting**. An account self-labeled "momentum" but showing FF3 t(MOM)=0.5 and t(SMB)=2.5 is actually a small-cap strategy in disguise — its risk management, sizing, and capital allocation should reflect that.

### 8.3 Set realistic expectations for B-group (GP)
Next article runs the same pipeline on B-group. Based on this article's lesson: **short-term flashy P&L almost certainly fails statistical alpha tests**. That's not a GP-algorithm failure; it's a sample-size law of physics.

### 8.4 What can actually move the needle
- **Longer track record**: be patient, 5+ years before significant α is even possible.
- **Lower account volatility**: via tighter sizing, drop vol from 50% to 15% → t-stat ~3.3× higher. This is why most hedge funds run very tight vol.
- **Combine accounts**: equal-weighting A-group 10 accounts diversifies idiosyncratic noise; A-Composite t-stat will be easier to clear 2.
- **Find genuinely uncorrelated alpha sources** — not yet another Alpha158 subset under a different composite recipe.

---

## 9. One-Line Summary

**Full multi-factor attribution on 20 paper accounts: 0 have statistically significant alpha.** The flashy YTD numbers are mostly small-cap style exposure (FF3 SMB β significant) — riding a wave, not skill. This isn't shameful; it's industry baseline. But naming it honestly beats pretending we have alpha we can't prove.

---

## 10. Reproducing

Code: `research/agroup_attribution.py` (~330 lines, includes SMB/MOM factor construction)
Inputs: `data/trading.db` 1d prices + `config/settings.py` US/CN universe
Run: `source venv/bin/activate && python research/agroup_attribution.py` — 2-3 minutes for all 4 matrices.
Raw outputs: [summary_capm.csv](summary_capm.csv) · [summary_ff3.csv](summary_ff3.csv)
