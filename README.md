# Cyber Quant Arena · 量化交易仪表盘

> 一个长期运行的纸面交易竞技场：30+ 个虚拟账户在 US / CN 两个市场里自动交易，dashboard 负责把它们的生死、收益、交易、因子和错误全部摊开给人看。

**这不是真钱交易界面，也不是投资建议。** 它是 [quant-trading](https://github.com/nullptr0807/quant-trading) 的只读可视化层：交易引擎写入 `trading.db`，这个 dashboard 读取同一份数据并展示。

## 1. 你打开它会看到什么

| 页面 | 看什么 | 适合回答的问题 |
|---|---|---|
| **Trading Overview** | 全账户收益分布、equity curve、Active / Retired 账户卡片、单账户抽屉 | 哪些策略活得好？谁跑赢/跑输基准？某账户为什么这么走？ |
| **Backtest Analysis** | 指定账户 + 时间窗的历史回放、交易点、统计指标 | 这个账户如果从某天开始跑，表现如何？ |
| **Factor Lab** | 自定义 Alpha158 风格因子组合，不创建账户，直接测 Rank IC / 分层收益 / equity | 一个手写因子想法有没有预测力？ |
| **Symbols** | 按 ticker 聚合所有账户交易，价格图上叠加多账户买卖点 | 大家都在交易哪只票？这只票总体赚还是亏？ |
| **Explore / Frontier** | 研究文章、论文前沿摘要 | 最近有哪些想法值得看？ |
| **Live Event Stream** | 交易、退役、调仓、风控、系统事件实时流水 | 系统刚刚做了什么？哪里出错了？ |
| **Intro** | 本文档的 dashboard 内嵌版 | 给完全没接触过量化的人解释系统 |

## 2. 两个市场同屏对照

顶部市场 tab 可以在 **US** 和 **CN** 之间切换。所有金额、账户、benchmark、ticker 名称和 API 请求都会跟随市场切换。

| | US | CN A-share |
|---|---|---|
| 股票池 | Russell 1000 | 沪深 300 |
| 初始资金 | `$10,000` / account | `¥100,000` / account |
| 基准 | QQQ + SPY | 沪深300 `000300.SH` |
| 账户 | A / B / F / Q / IDX | CA / CB / CF / CQ / IDX3 |
| A 股特殊规则 | — | 买入必须 100 股一手；买不起会记录 skip event |

CN 账户是 US 策略的镜像，用来观察同一类 alpha 在不同市场结构里的可迁移性。

## 3. 账户视角：策略赛马

Trading Overview 的核心是账户。

### 收益分布而不是单一平均数

首页 hero 不只看一个总收益，而是展示：

- median / mean / best / worst
- IQR 分位区间
- win rate
- 每个账户的收益直方图

原因很简单：30+ 个策略并行时，平均数会掩盖幸存者偏差。一个账户暴涨不代表系统整体有效；分布才是事实。

### Active / Retired

账户不是永远活着：

- **Active**：仍在接收信号、调仓、记录持仓
- **Retired**：停止交易，但历史曲线、交易、退役原因全部保留

Retired tab 是墓碑墙。它不是“删掉失败策略”，而是保留失败证据：因子重复、长期跑输、回撤过大、实验完成，都应该被记录。

### 单账户抽屉

点开账户可以看到：

- equity curve + benchmark overlay
- 当前持仓和最近交易
- 因子公式、策略解释、GP/FactorMiner 状态
- Signal Quality：Rank IC / ICIR / 覆盖率
- Factor AI：LLM 对账户因子和交易行为的解释（带缓存）

## 4. 标的视角：一只股票被谁交易过

Symbols tab 把视角从“账户”翻转成“股票”。

列表页显示每个 ticker：

- 有多少账户交易过
- 总交易次数
- 已实现 / 浮动 / 总盈亏
- 最近交易时间

详情页显示：

- 价格曲线 + 多账户买卖 marker
- hover 时看到具体账户、方向、股数、成交价
- 公司简介、行业、下次财报、官网
- 同行业 peer chips
- 每账户 FIFO 账本和盈亏拆解

这页回答的是：**某只股票到底给系统贡献了 alpha，还是拖了后腿？**

## 5. 因子实验室：不建账户也能测想法

Factor Lab 是轻量研究台，不写 `trading.db`，只读价格数据。

你可以组合：

- ROC / MA_RATIO / BETA / VMOM / VSTD / STD / BBPOS / RSV / RSI
- 自定义 periods，例如 `ROC[7,11]` 或 `BETA[5,10,20]`
- rank / zscore transform
- rebalance days、hold-band、cooldown、min-hold 等执行参数

输出包括：

- Rank IC / rolling ICIR
- quantile returns
- top-N portfolio equity
- 最新 top-ranked names
- look-ahead / survivorship / scale warnings

它适合回答：**这个因子想法在变成账户之前，是否值得继续？**

## 6. 回测页：历史重放，不等于真实未来

Backtest Analysis 可以选择账户和时间窗，把策略在历史区间里重放。

它会展示：

- equity curve
- buy / sell markers
- trade table
- realized stats（win rate、profit factor 等）
- benchmark 对照

Qlib 账户有专门的状态 banner：当模型 checkpoint 覆盖不足、存在 look-ahead 风险时，页面会明确告诉你哪些账户被 blocking，而不是偷偷跑一个假的回测。

## 7. Live Event Stream：系统的黑匣子记录

事件流把系统动作按时间倒序展示：

- `trade`：买入 / 卖出 / stop-loss
- `rebalance`：调仓周期触发
- `system`：cron、watchdog、git commit、特殊跳过事件
- `retire` / lifecycle：账户退役、复活、创建
- `data` / `factor`：数据和因子处理事件

最近新增的 A 股一手制也会落事件：如果某个 CN 账户因为 `100股 × price` 超过预算或仓位上限而跳过 ticker，会显示类似：

```text
⏭️ Skip 600519.SH: 1手买不起/超仓位 (budget ¥20000, 1手≈¥119556)
```

这样你不只知道“没买”，还知道**信号曾经出现过，只是执行约束不允许**。

## 8. 这套 dashboard 不是什么

- **不是**交易下单界面。它不会把订单发给券商。
- **不是**研究结论生成器。它展示事实，解释仍需要人判断。
- **不是**只看胜利者的宣传页。退役账户、错误事件、watchdog warning 都会保留。
- **不是**黑箱。每个账户、ticker、交易、因子、事件都可以追溯。

它的目标是：让长期运行的量化实验**可观察、可质疑、可复盘**。

## 9. 数据来源与责任边界

```
quant-trading 负责：行情 → 因子 → 信号 → 模拟成交 → 账本 / events
trading-dashboard 负责：读取同一个 trading.db → 可视化 → 交互分析
```

dashboard 默认只读。真正改变账户状态的操作（交易、退役、修复、回放）应发生在 quant-trading 侧，并写入事件留痕。

## 10. 免责声明

MIT。仅供学习研究。所有交易都是模拟成交；任何基于本项目做出的真实投资决策，风险自担。
