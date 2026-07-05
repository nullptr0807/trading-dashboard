# Cyber Quant Arena · Quant Trading Dashboard

> A live paper-trading arena: 30+ virtual accounts trade automatically across US and CN markets, while this dashboard exposes their returns, trades, factors, lifecycle events, and failures.

**This is not a real-money trading interface and not investment advice.** It is the read-only visualization layer for [quant-trading](https://github.com/nullptr0807/quant-trading): the engine writes `trading.db`; this dashboard reads the same database and renders it for humans.

## 1. What you see when you open it

| Page | What it shows | Questions it answers |
|---|---|---|
| **Trading Overview** | Account return distribution, equity curves, Active / Retired cards, account drawer | Which strategies are alive? Who beats the benchmark? Why did one account move? |
| **Backtest Analysis** | Historical replay for chosen accounts and date windows | What would this account have done from a given start date? |
| **Factor Lab** | Ad-hoc Alpha158-style factor experiments without creating accounts | Does this handcrafted factor idea have predictive power? |
| **Symbols** | Aggregates every account's trades by ticker | Which stocks does the system trade most? Did a ticker help or hurt? |
| **Explore / Frontier** | Research notes and paper digests | What ideas are worth reading next? |
| **Live Event Stream** | Trades, retirements, rebalances, risk, and system events | What did the system just do? What broke? |
| **Intro** | The in-app version of this documentation | A beginner-friendly explanation of the system |

## 2. Two markets, one interface

The top market tabs switch between **US** and **CN**. Currency, accounts, benchmarks, ticker names, and API requests follow the selected market.

| | US | CN A-share |
|---|---|---|
| Universe | Russell 1000 | CSI 300 |
| Starting capital | `$10,000` / account | `¥100,000` / account |
| Benchmark | QQQ + SPY | CSI 300 `000300.SH` |
| Accounts | A / B / F / Q / IDX | CA / CB / CF / CQ / IDX3 |
| A-share execution rule | — | Buys must be multiples of 100 shares; unaffordable signals become skip events |

CN accounts mirror US strategy families so the same alpha idea can be compared across two very different market structures.

## 3. Account view: strategy horse race

Trading Overview is account-centric.

### Distribution, not just average return

The hero section shows:

- median / mean / best / worst
- IQR range
- win rate
- account-level return histogram

With 30+ strategies running in parallel, an average can hide survivor bias. One lucky account does not prove the whole system works; the distribution is the evidence.

### Active and Retired

Accounts have a lifecycle:

- **Active**: still receiving signals, rebalancing, and writing position snapshots
- **Retired**: no longer trades, but historical equity, trades, and retirement reason remain visible

The Retired tab is a tombstone wall. It deliberately preserves failures: duplicate factors, long-term underperformance, excessive drawdown, and finished experiments are all research evidence.

### Account drawer

Click an account to inspect:

- equity curve with benchmark overlays
- current holdings and recent trades
- factor formulas, strategy explanation, GP / FactorMiner status
- Signal Quality: Rank IC / ICIR / coverage
- Factor AI: cached LLM commentary on factors and behavior

## 4. Symbol view: who traded this stock?

The Symbols tab flips the perspective from account to ticker.

The list page shows each traded ticker's:

- number of accounts involved
- number of trades
- realized / unrealized / total PnL
- most recent trade timestamp

The detail page shows:

- price chart with multi-account buy/sell markers
- hover tooltip with account, side, shares, and fill price
- company profile, sector, industry, next earnings date, and website
- same-industry peer chips
- per-account FIFO ledger and PnL breakdown

This answers: **did this stock create alpha for the system, or did it drag returns down?**

## 5. Factor Lab: test an idea before creating an account

Factor Lab is a lightweight research surface. It is read-only and does not write to `trading.db`.

You can combine:

- ROC / MA_RATIO / BETA / VMOM / VSTD / STD / BBPOS / RSV / RSI
- custom periods, e.g. `ROC[7,11]` or `BETA[5,10,20]`
- rank / z-score transforms
- execution knobs such as rebalance days, hold-band, cooldown, and minimum holding days

Outputs include:

- Rank IC / rolling ICIR
- quantile returns
- top-N portfolio equity
- latest top-ranked names
- look-ahead / survivorship / scale warnings

It is meant to answer: **is this factor idea worth turning into a real account?**

## 6. Backtest page: replay, not prophecy

Backtest Analysis replays selected accounts over a historical window.

It shows:

- equity curve
- buy / sell markers
- trade table
- realized stats such as win rate and profit factor
- benchmark comparison

Qlib accounts include a status banner. If model checkpoints are incomplete or a replay would risk look-ahead bias, the UI blocks the account and explains why instead of silently running a fake backtest.

## 7. Live Event Stream: the system's black-box recorder

The event stream is the chronological audit trail:

- `trade`: buy / sell / stop-loss
- `rebalance`: rebalance trigger
- `system`: cron, watchdog, git commit, special skip event
- `retire` / lifecycle: account creation, retirement, resurrection
- `data` / `factor`: data and factor-processing events

The A-share board-lot constraint is also visible here. If a CN account skips a ticker because `100 shares × price` exceeds its budget or position cap, an event appears like:

```text
⏭️ Skip 600519.SH: board lot unaffordable / above position cap (budget ¥20000, 1 lot≈¥119556)
```

So you can distinguish **"the signal never appeared"** from **"the signal appeared, but execution constraints made it untradeable."**

## 8. What this dashboard is NOT

- **Not** an order-entry system. It never sends orders to a broker.
- **Not** a research conclusion machine. It shows evidence; interpretation still belongs to humans.
- **Not** a winner-only marketing page. Retired accounts, errors, and watchdog warnings remain visible.
- **Not** a black box. Every account, ticker, trade, factor, and event is traceable.

Its purpose is to make a long-running quant experiment **observable, debatable, and replayable**.

## 9. Data boundary

```
quant-trading: market data → factors → signals → simulated execution → ledger / events
trading-dashboard: read the same trading.db → visualize → interactive analysis
```

The dashboard is read-only by default. Any operation that changes account state — trading, retirement, repair, replay — should happen on the quant-trading side and leave an event trail.

## 10. Disclaimer

MIT. For learning and research only. All trades are simulated; any real investment decision made from this project is your own responsibility.
