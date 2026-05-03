# 仓位控制：业界做法、对照实验与反直觉的结论

> 探讨日期：2026-05-03  ·  作者：Cyber Quant Arena  ·  分类：Risk / Portfolio Construction

## 1. 提出的问题

在我们这套美股模拟交易系统里，每个账户买入一只股票时**到底花多少钱**？这看似一个简单的工程问题，背后实际上是 **portfolio construction（组合构建）**——量化交易里和 alpha 信号同等重要的一环。

我们当前的逻辑（`trading/engine.py` + `main.py::_trade_*_account`）大致是：

```
target_per_position = equity × max_position_pct
budget              = min(target − held_value,  cash × 0.95)
shares              = int(budget / price)
```

再加上引擎层 30% 单股集中度兜底。本质上是**等权 + 比例上限**的简单方案。

那么业界是怎么做的？我们能不能照搬一个更"成熟"的方案过来？

---

## 2. 业界的工具箱

把日频量化的仓位控制按"从粗到细"展开，常见的有四层，业界一般是 **多层叠加**，而不是单选一个。

### 2.1 组合构建层：决定"每只股票占多少"

| 方法 | 公式 | 谁在用 |
|---|---|---|
| **等权 / Top-N** | `w_i = 1/N` | AQR、Research Affiliates 早期因子组合 |
| **信号加权** | `w_i ∝ z(α_i)` | 中频多因子（Barra 客户） |
| **风险平价 / 1/σ** | `w_i ∝ 1/σ_i` | Bridgewater、CTA 行业（AHL/Winton） |
| **均值-方差优化（MVO）** | `max  μᵀw − λ·wᵀΣw` | Two Sigma、AQR、Millennium PM 层 |
| **Fractional Kelly** | `w* ∝ Σ⁻¹·μ`（½ 或 ¼） | Renaissance、Two Sigma 内部研究 |
| **Black-Litterman** | 先验 + alpha 信号贝叶斯融合 | 高盛资管、PIMCO |

**关键点**：MVO 里的 μ 用 alpha 信号、Σ 用 **多因子风险模型**（Barra/Axioma）而不是历史样本协方差——后者在高维下噪声爆表。

### 2.2 风险约束层：MVO 之上的硬约束

业界 PM 真正下单时，优化问题里 **永远** 带这些约束：

- 单股权重 2–5%（多头）/ 1–3%（多空）
- 单行业敞口 ±3% vs benchmark
- 单风格因子敞口（Size/Value/Mom/Beta）±0.3σ
- 组合 Beta 0.95–1.05（多头）/ ±0.1（市场中性）
- 跟踪误差 3–6%/年
- 总杠杆 100/100 多空
- 单股流动性 ≤ ADV 的 5–10%

### 2.3 信号→目标仓位的转换

- **截面排名 → MVO**（多因子选股标准管线）
- **时间序列信号 → 波动率目标**（CTA 趋势）：`position = signal × target_vol/σ × capital`
- **组合层 Vol Targeting**：`leverage = target_portfolio_vol / realized_vol`
- **Risk Budgeting**：给每个子策略/PM 分配 **风险预算**（VaR 或 vol 贡献）而不是资金额度（Millennium / Citadel 的 pod 模型）

### 2.4 交易成本与换手控制

```
max  μᵀw − λ_risk·wᵀΣw − λ_tcost·|w − w_prev|^1.5
```

平方根冲击成本（Almgren-Chriss）+ 换手限制（日 ≤ 10–20%）。没有这层，日频策略 IR 通常会被交易成本吃掉一半。

---

## 3. 实验设计

把所有"理论"摆完之后，关键问题是：**这些方法在我们的 universe、我们的 alpha 信号上真的有效吗？**

为此设计一个 **干净的对照实验**：

| 维度 | 设置 |
|---|---|
| 数据 | `trading.db` 日线，2021-01-01 → 2026-05-01 |
| 股票池 | Russell 1000 中日均成交额 > $5M 的 2336 只 |
| Alpha 信号 | 20 日动量 `ROC_20`（**所有方案完全一致**） |
| 持仓构建 | 每日选 top-20，纯多头，下一日开盘建仓 |
| 交易成本 | 5bps × 换手 |
| 初始资金 | $10,000 |
| **唯一变量** | 仓位算法 |

### 对照四种仓位方案

1. **EW** —— 等权（业界 baseline，也对应我们当前的逻辑）
2. **IV** —— 1/σ 加权（σ = 20 日实现波动率）
3. **EW + VT** —— 等权 + 组合层 Vol Targeting（年化目标 12%，杠杆上限 1.5×）
4. **IV + VT** —— 风险加权 + Vol Targeting

VT 的 leverage 估算（朴素假设跨股零相关，仅用于 sizing 旋钮）：

```
est_port_vol = sqrt(Σ wᵢ²·σᵢ²) × √252
leverage     = min(target_vol / est_port_vol, 1.5)
```

---

## 4. 实验过程

实现脚本：[`analysis/sizing_experiment.py`](https://github.com/) — pandas 向量化，单 ticker pivot → 截面 rank → 每日下一根 close-to-close 回报。

跑完四个方案，得到下面这张图：

![sizing comparison](sizing_comparison.png)

---

## 5. 结果

| 方案 | 总收益 | 年化 | 波动率 | Sharpe | 最大回撤 | Calmar | 平均换手 | 平均杠杆 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **EW**（baseline）| **+66.4%** | 10.1% | 33.8% | **0.30** | -49.2% | 0.20 | 0.47 | 1.00 |
| **IV** | +58.3% | 9.1% | 31.5% | 0.29 | -47.6% | 0.19 | 0.58 | 1.00 |
| **EW + VT** | +21.3% | 3.7% | 19.5% | 0.19 | -37.3% | 0.10 | 0.34 | 0.64 |
| **IV + VT** | −1.7% | −0.3% | 24.1% | −0.01 | -46.9% | −0.01 | 0.48 | 0.86 |

**反直觉的结论：业界两个常用工具（InvVol、VolTargeting）都没跑赢简单等权。**

---

## 6. 怎么解读

### 6.1 InvVol 没赢 EW
1/σ 加权理论上让风险更均匀，但在 **动量组合** 里：动量 + 高波动 强相关——低波动票本身收益就低。InvVol 把高波动票权重压低，等于砸了自己的 alpha 暴露。波动率（33.8% → 31.5%）和回撤几乎没改善，但收益掉了 8 个百分点。

### 6.2 VolTarget 严重伤害收益
平均杠杆只有 **0.64**——top-20 动量组合的实现波动率长期高于 12% 目标，VT 一直在缩仓。结果：vol 从 34% 砍到 19.5%（确实降了），但年化收益从 10% 砍到 3.7%。**Sharpe 反而变差**（0.30 → 0.19）。

### 6.3 IV + VT 最差
两个工具叠加，把已经稀薄的 alpha 削得太狠。

---

## 7. 业界经验为什么在这里失效

几个真实原因：

1. **Alpha 信号太弱**：20 日动量 IR ≈ 0.3，本来就没多少 alpha 可榨。所有"风险更平滑"的努力都是在拿 return 换 vol，对应不上 Sharpe 提升。
2. **Top-N 动量天然集中在高波动票**（科技股、小盘成长），InvVol 把它们权重压低 = 砸自己的脚。
3. **VolTarget 的目标 12% 太低**——该组合的"自然波动率"是 30%+，目标设这么低意味着长期空仓 36% 资金。
4. **没有空头腿**：业界 vol targeting 真正发威是在 **市场中性多空组合**（vol 自然较低、20 年偶尔尖峰，那时 VT 救命）。纯多头组合 vol 跟着大盘走，VT 主要在压 beta 而不是压"特殊风险"。

---

## 8. 启示与下一步

这个实验给我们一个反直觉但有价值的结论：

> **业界做 vol-targeting / 风险加权之所以有效，是因为它们用在了 IR ≥ 0.5、Sharpe ≥ 1.0 的好信号上。对一个 Sharpe ≈ 0.3 的弱信号，再花哨的风控只会把已经稀薄的 alpha 进一步稀释。**

### 对当前系统的真实建议

- ❌ **不要立刻给 A/B 账户加 InvVol 或 VolTarget**——会无端损失收益。
- ✅ 如果想做风险控制，**目标应该是回撤而不是波动**：例如账户净值跌 8% → 仓位 ×0.5（drawdown stop），这是非对称的、不损 alpha。
- ✅ 真正高 ROI 的方向还是 **alpha 质量**（让 Q 系列的模型预测更准、给 GP 更长训练窗口），而不是 sizing。
- ✅ 如果某天我们做出多空组合，再回来谈 vol-targeting。

### 后续可做的对照实验

- **EW vs EW + Drawdown Stop**（账户级回撤刹车）
- **Top-N 动量 vs Top-N 多因子**（现在 Sharpe=0.3，看看多因子能否拉到 0.5+ 之后再谈风控）
- **多头 vs 多空中性**（IR 提升后，vol-targeting 是否就能发挥作用）

---

*实验脚本：`~/quant-trading/analysis/sizing_experiment.py`  ·  原始结果：`/tmp/hermes/sizing_exp/summary.csv`*
