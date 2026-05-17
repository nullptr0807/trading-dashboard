# Cyber Quant Arena · Quant Trading System

> Written for someone who has never touched quantitative finance.

## 1. What is this thing? (one minute)

We are **not actually trading**. This is a **paper-trading arena**:

- Each virtual account starts with seed capital (US $10,000 per account, CN ¥100,000)
- Each account runs a **different strategy** — a different "belief" about how stocks move (e.g. "buy what's going up" vs. "buy what's gone down too far")
- Every day, on a cron schedule, the system pulls real market data and lets each strategy **decide on its own** what to buy, sell, and how much
- We **don't intervene** — we just watch the virtual accounts go up and down
- Strategies that perform poorly get **retired** (they become tombstones), proving they wouldn't survive in the real market

Why? Before risking real money in the market, we run **multiple competing strategies** on real-time and historical data as a controlled experiment, to see which "belief" actually works long-term. This is the classic quant-research **horse-race** approach: rather than betting on one strategy, raise twenty and let the market tell you which ones survive.

## 2. The two markets: US and CN

| | US | CN |
|---|---|---|
| **Universe** | Russell 1000 (~1004 stocks, GICS-sectored) | CSI 300 (300 large-cap A-shares) |
| **Currency** | USD $ | CNY ¥ |
| **Per-account seed** | $10,000 | ¥100,000 |
| **Data source** | yfinance (Yahoo, free) + Finnhub (live quotes) | akshare (open A-share data API) |
| **Benchmark** | QQQ (Nasdaq-100) + SPY (S&P 500) | CSI 300 (000300.SH) |
| **Trading hours** | 9:30 – 16:00 EST | 9:30 – 11:30 / 13:00 – 15:00 Beijing |
| **Cost model** | moomoo AU fees (the user's actual broker) | 2.5 bps + stamp duty |
| **Account prefix** | `A01-A10` / `B01-B16` / `Q01-Q10` / `IDX1-2` | `CA01-CA10` / `CB01-CB16` / `CQ01-CQ10` / `IDX3` |

CN accounts are a **mirror** of US accounts: same strategy ideas, ported to the A-share universe. The `C` prefix marks them as Chinese. The motivation: contrast the same strategy across two structurally very different markets (T+1 vs T+0, daily price limits vs free movement, retail-driven vs institutional-driven) — is the alpha real, or is it a market-structure artifact?

## 3. The three account classes: A / B / Q

Each market has 3 classes (plus IDX benchmarks), totaling 30+ accounts. This is the **core design**:

### A class · Alpha158 (handcrafted rules)

> "Textbook factors — every one has a name, every one is human-readable"

- **A01–A10** (US) / **CA01–CA10** (CN), 10 accounts each
- Each account uses a curated **classical factor combination** as its signal
- Factors come from Microsoft Qlib's **Alpha158** suite (a 10+ year industry-standard factor library), covering:
  - **Price-volume**: momentum, reversal, volatility, volume ratio, turnover
  - **Moving averages**: 5/10/20/60-day MA deviations
  - **Trend**: breakouts, drawdowns, relative strength
  - **Statistical**: skewness, kurtosis, autocorrelation
- Every factor has a **mathematical formula** and **explanation** (click any account in the dashboard to see KaTeX-rendered formulas)
- Examples: `A01 Momentum Alpha` = "the N stocks that ran the hardest the past 20 days"; `A02 Mean Reversion` = "the most beaten-down, short-term oversold stocks"

**Why A?** A = Alpha158. **They are the baseline** — they tell us how much money the best human-known factors can make.

### B class · Genetic Programming (machine-discovered factors)

> "We don't know why it works, but the genetic algorithm evolved it"

- **B01–B16** (US) / **CB01–CB16** (CN), 16 accounts each (many retired)
- Use **Genetic Programming (gplearn)** to **automatically search** factor expressions on historical data
- Give the algorithm 13 atomic building blocks (open, close, volume, 5/10/20-day MA, volatility, returns…) and let it **freely combine** them: add, subtract, multiply, divide, log, abs, min, max, sliding mean…
- Run 50 generations of evolution: each generation eliminates half, mutates/crosses survivors, keeps the highest-IC (information coefficient) expressions
- Example: B01 might evolve something like `mul(div(std_5, ma_20), sub(ret_5, ret_10))`. A human can't read this directly.
- The dashboard's `core/gp_explain.py` module **auto-translates** these trees into English: "This factor measures short-term volatility relative to mid-term moving average, multiplied by a medium-term reversal signal"
- Each B account uses different evolution hyperparameters (population size, tournament size, max tree depth, IC weighting, holdings count, rebalancing frequency), so the discovered factors have different styles

**Why B?** B = Bottom-up search. **They are exploration** — can the machine discover money-making patterns humans missed?

#### The B class "tombstone wall"

B01–B10 was the **first generation** of GP miners. In late April, after a factor-clustering dedup pass, accounts that evolved **similar alphas** (e.g. 6 highly-correlated factors all in the same momentum cluster) were **retired**:

- B02 / B04 / B06 retirement reason: `GP factor cluster dedup: momentum cluster (kept B01)` — evolution converged, kept B01 as the cluster representative
- B07 retired: `short-momentum cluster (kept B05)`
- B09 retired: `vol+volume cluster (kept B03)`
- …

This is a real-world quant practice: **factor redundancy = concentrated risk**, must dedup. Retired accounts are memorialized on the dashboard as **tombstones**, with retirement date, lifetime return, and retirement reason ("epitaph") — both as research record and as a tribute to the short-lived strategies.

B11–B16 are the **second generation**, with finer evolution objectives (Sharpe maximization, drawdown resistance, price-volume resonance), so their names are more thematic (Short-Burst Hunter, Sharpe Hunter, Drawdown Guard…).

### Q class · Qlib models (machine-learning models)

> "Feed all 158 factors to LightGBM and let the model learn the weights"

- **Q01–Q10** (US) / **CQ01–CQ10** (CN), 10 accounts each
- Built on Microsoft [Qlib](https://github.com/microsoft/qlib)'s standard model zoo
- Use Alpha158's **158 factors** as input features to predict next-period return rankings
- Each Q account runs a **different model architecture**:

| Account | Model | Type |
|---|---|---|
| Q01 | LightGBM Ranker | Tree |
| Q02 | XGBoost Ranker | Tree |
| Q03 | CatBoost Ranker | Tree |
| Q04 | Ridge Linear | Linear |
| Q05 | MLP | Shallow neural net |
| Q06 | LSTM | Recurrent |
| Q07 | GRU | Recurrent |
| Q08 | Transformer | Attention |
| Q09 | TCN | Temporal convolution |
| Q10 | ALSTM | LSTM + attention |

- **Every day at 23:00 UTC**, models are **rolling-retrained** on the past N days; new predictions overwrite old ones
- Predicted scores live in `factor_values.qlib_QXX_score` (CN: `qlib_CQXX_score`)
- Each Q account holds the Top-N stocks by score

**Why Q?** Q = Qlib. **They are the industry benchmark** — do ML models really beat handcrafted factors or GP evolution?

### IDX class · Benchmark indices (not strategies)

- **IDX1** = QQQ (Nasdaq-100 ETF)
- **IDX2** = SPY (S&P 500 ETF)
- **IDX3** = CSI 300 index (000300.SH)

These aren't strategy accounts — they're **benchmarks**. The dashboard's Equity Curve overlays them on top of every strategy curve, so you can immediately see "my strategy beat SPY by X% / lost to SPY by Y%". Any strategy that **can't beat SPY long-term** has no reason to exist (just buy the ETF and call it a day).

## 4. What happens in a day? (typical cron rhythm)

```
Pre-market
├── Price data refresh (akshare/yfinance: yesterday's close + latest 1d/1h bars)
├── Factor recomputation (full Alpha158, GP expressions, Qlib model inference)
└── Signal generation (each account scores via its own factors → rebalance list)

Intraday (every 15-30 min cron tick)
├── Pull live quotes (yfinance fast_info / akshare 1-min bar)
├── Check signal changes, stop-loss / take-profit triggers
├── Simulated execution (apply moomoo AU fee model)
└── Mark-to-market (revalue every account's equity at latest price)

Post-market
├── Write daily snapshot → accounts table (one row = account at a moment in time)
├── Write position snapshots → snapshots table (powers the hover tooltip)
└── 23:00 UTC: Qlib models rolling-retrain (10 Q accounts re-trained)

Real-time
└── lifecycle / risk / error events → events table (live-streamed in the dashboard)
```

The whole pipeline is **fully automated**. The dashboard itself **doesn't decide anything** and **doesn't write any data** — it just visualizes the result.

## 5. Account lifecycle

- **Birth**: a row is INSERTed into `account_meta` with `created_at`, seed capital, group, strategy name, description. Then cron starts computing factors, generating signals, recording daily equity for it.
- **Alive**: every day brings new trades, new snapshots, new equity points. The "Active" tab on the dashboard lists living accounts sorted by return.
- **Retirement**: `account_meta.status = 'retired'`, with `retired_at` timestamp and `retire_reason` (the epitaph). The system **stops** generating new signals and stops trading for it, but **keeps all historical data** for replay and comparison. The "Retired" tab shows the tombstone wall.
- **Resurrection**: in principle, flipping status back to active brings it back (quant-trading has `retire_account / unretire_account` commands). But typically a retired account is left alone — its history itself is the research record.

Common retirement reasons:
1. **Factor redundancy** (multiple accounts in the same cluster evolved correlated alphas) → keep one, tombstone the rest
2. **Long-term underperformance** (Sharpe < 0, return below SPY) → strategy proven ineffective
3. **Drawdown breach** (max drawdown exceeded preset threshold) → risk-control trigger
4. **Test accounts** (e.g. `C01 test strategy`) archived after research goal achieved

## 6. The Symbols Tab

The **Symbols** tab inverts the dashboard's perspective — instead of grouping by account, it aggregates every account's activity by ticker.

**List view**: one row per traded ticker, showing # accounts, # trades, realized PnL, and last-trade date. All four numeric columns are click-to-sort, plus a search box for quick lookup.

**Detail view** (click any ticker):

- **Price chart with multi-account trade markers**: each account that traded the ticker gets its own color; buy/sell arrows mark every fill on the price line; hover reveals account, side, shares, price
- **Company profile card** (right side): full name, GICS sector / industry, 2-3 sentence business summary, next earnings date, website. Auto-switches EN/ZH based on language toggle (Google Translate, disk-cached so repeat lookups are free)
- **Similar companies**: same-GICS-industry peers from the Russell 1000 universe (e.g. NVDA → AMD, AVGO, MU, INTC, QCOM, ...) — click any chip to navigate
- **Per-account PnL table**: each account's strategy, trade count, realized / unrealized / total PnL, return %, current holding. Click a row to expand a FIFO ledger showing per-trade PnL and running position

Data sources: yfinance for profile + industry classification, the Russell 1000 universe for the peer pool.

## 7. Recommended reading paths (by role)

- **Total beginner** → click any A-class account, look at the factor cards — every factor has a formula + plain-English motivation
- **Quant newcomer** → compare A (handwritten) / B (GP-mined) / Q (ML-learned) curves over time — which class wins long-term?
- **Researcher** → look at the B-class tombstone wall + retirement reasons; this is real factor-dedup / exploration / failure record
- **Trader** → compare same-named strategies across markets (A01 vs CA01) to test cross-market portability
- **Want to validate an idea** → use the Backtest Analysis tab: pick an account, set a time window, replay history

## 8. What this system is NOT

- **Not** a real-money trading system. All execution is in-memory, simulated against historical/live prices.
- **Not** high-frequency. The fastest cron tick is minute-level; factors run on daily/hourly bars.
- **Not** long-short. All accounts are **long-only** — closer to retail-style long-term alpha stock picking.
- **Not** an automated research platform. Adding strategies, tuning hyperparameters, changing risk controls all still require code edits + cron restart.

But it **is**: a real, long-running, multi-strategy controlled experiment with full data lineage and reproducibility. Daily PnL is driven by real market data. Every trade in every account — what was bought, sold, when, and why — is fully traceable.
