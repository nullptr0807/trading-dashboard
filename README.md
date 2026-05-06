# Cyber Quant Arena · 量化交易系统介绍

> 写给完全没接触过量化的人。

## 1. 这个系统在干什么？（一分钟版本）

我们**没有真的在交易**。这是一个**纸面交易竞技场**：

- 一开始，给每个虚拟账户发一笔本金（美股 $10,000，A 股 ¥100,000）
- 每个账户跑一个**不同的策略**（一种炒股的"信仰"，比如"追涨" vs "抄底"）
- 系统每天定时（cron）拉取真实的市场行情，让每个策略**自己决定**买什么、卖什么、各买多少
- 我们**不动手**，只看着这些虚拟账户的钱涨涨跌跌
- 跑得不好的账户会被**退役**（变成墓碑），证明这个策略在真实市场里活不下去

为什么要这么做？—— 在拿真钱去市场之前，先用**多个互相竞争的策略**在历史和实时数据上做对照实验，看哪一种"信仰"长期有效。这是经典量化研究的"赛马"思路：与其相信一个策略，不如同时养 20 个，让市场告诉你哪个能活。

## 2. 两个市场：US（美股）和 CN（A 股）

| | US | CN |
|---|---|---|
| **标的池** | Russell 1000（约 1004 支美股，按 GICS 行业分类） | 沪深 300（300 支 A 股大盘股） |
| **币种** | 美元 $ | 人民币 ¥ |
| **每个账户初始资金** | $10,000 | ¥100,000 |
| **行情来源** | yfinance（雅虎财经，免费）+ Finnhub（实时报价补全） | akshare（A 股开源数据接口）|
| **基准指数** | QQQ（纳斯达克 100）+ SPY（标普 500） | 沪深 300（000300.SH）|
| **交易时段** | 美东 9:30 – 16:00 | 北京 9:30 – 11:30 / 13:00 – 15:00 |
| **成本模型** | moomoo AU 费率（用户实际经纪商）| 万 2.5 + 印花税 |
| **账户前缀** | `A01-A10` / `B01-B16` / `Q01-Q10` / `IDX1-2` | `CA01-CA10` / `CB01-CB16` / `CQ01-CQ10` / `IDX3` |

CN 账户就是 US 账户的**镜像**：同样的策略思路，跑在 A 股池子上。前缀加 `C` (China) 区分。设计动机：对照同一个策略在两个完全不同制度（T+1 vs T+0、涨跌停 vs 自由波动、散户主导 vs 机构主导）的市场上的表现差异 —— 是真 alpha 还是市场结构红利？

## 3. 三大类账户：A / B / Q

每个市场都有 3 类（外加 IDX 基准）共 30+ 个账户。这是整个系统的**核心架构**：

### A 类 · Alpha158（手写规则派）

> "教科书上的因子，每一个都有名字，每一个都看得懂"

- **A01–A10** (US) / **CA01–CA10** (CN)，各 10 个账户
- 每个账户用一个**经典量化因子组合**作为信号源
- 因子来自微软 Qlib 的 **Alpha158** 套件（学界/业界用了 10+ 年的标准因子库），涵盖：
  - **量价类**：动量、反转、波动率、量比、换手率
  - **均线类**：5/10/20/60 日均线偏离度
  - **趋势类**：突破、回撤、相对强弱
  - **统计类**：偏度、峰度、自相关
- 每个因子都有**数学公式**和**中文动机解释**（在仪表盘点开账户能看到 KaTeX 渲染的公式）
- 例：`A01 动量Alpha` = "过去 20 天涨得最猛的 N 支"，`A02 均值回归` = "跌得最狠且短期超卖的 N 支"

**为什么叫 A 组？** A = Alpha158。**它们是基线**，告诉我们"用人类已知的最好的因子能赚多少"。

### B 类 · GP 进化派（机器自己挖出来的因子）

> "我们不知道为什么有效，但遗传算法让它进化出来了"

- **B01–B16** (US) / **CB01–CB16** (CN)，各 16 个账户（已退役不少）
- 用**遗传编程 (Genetic Programming, gplearn)** 在历史数据上**自动搜索**因子表达式
- 给算法 13 个原子积木（开盘价、收盘价、成交量、5/10/20 日均线、波动率、收益率…），让它**自由组合**：加减乘除、log、abs、min、max、sliding mean…
- 跑 50 代进化，每代淘汰一半、变异/交叉幸存者，最后留下 IC（信息系数）最高的表达式
- 例：B01 进化出来的可能长这样 `mul(div(std_5, ma_20), sub(ret_5, ret_10))`，人眼完全看不懂
- Dashboard 的 `core/gp_explain.py` 模块会把这种树自动**翻译成中文**："这个因子衡量短期波动率相对中期均线的强度，乘以中长期反转信号"
- 每个 B 账户用**不同的进化超参数**（种群大小、tournament 大小、最大树深、IC 加权方式、持仓数量、调仓频率），所以挖出来的因子风格各异

**为什么叫 B 组？** B = Bottom-up（自下而上的搜索）。**它们是探索**，看看机器能不能发现人类没想到的赚钱模式。

#### B 组的"墓碑墙"

B01–B10 这一批是**第一代** GP 矿工。dashboard 在 4 月底的一次因子聚类去重操作里，把进化出**类似 alpha**（比如同一个动量集群里挖出 6 个高度相关的因子）的账户**退役**：

- B02 / B04 / B06 退役原因：`GP factor cluster dedup: momentum cluster (kept B01)` —— 进化重了，留 B01 当代表
- B07 退役：`short-momentum cluster (kept B05)`
- B09 退役：`vol+volume cluster (kept B03)`
- …

这是真实研究里的常见操作：**因子重复就等于风险集中**，必须去重。退役的账户在仪表盘上以**墓碑**形式纪念，写明退役日期、终身收益、退役原因（"墓志铭"）—— 既是研究记录，也是对这些"短命策略"的致敬。

B11–B16 是**第二代**，用了更精细的进化目标（夏普比率最大化、抗回撤、量价共振…），所以名字也更"形象化"（短打猎手、夏普猎人、抗跌守卫…）。

### Q 类 · Qlib 模型派（机器学习模型）

> "把所有因子喂给 LightGBM，让模型自己学权重"

- **Q01–Q10** (US) / **CQ01–CQ10** (CN)，各 10 个账户
- 集成微软 [Qlib](https://github.com/microsoft/qlib) 的标准模型库
- 用 Alpha158 的 **158 个因子**作为输入特征，预测下一期收益排名
- 每个 Q 账户跑一个**不同的模型架构**：

| 账户 | 模型 | 类型 |
|---|---|---|
| Q01 | LightGBM 排序 | 树模型 |
| Q02 | XGBoost 排序 | 树模型 |
| Q03 | CatBoost 排序 | 树模型 |
| Q04 | Ridge 线性 | 线性模型 |
| Q05 | MLP 神经网 | 浅层神经网 |
| Q06 | LSTM 时序 | 循环神经网 |
| Q07 | GRU 时序 | 循环神经网 |
| Q08 | Transformer | 注意力机制 |
| Q09 | TCN 卷积时序 | 时序卷积 |
| Q10 | ALSTM 注意力 | LSTM + 注意力 |

- **每天 23:00 UTC** 用过去 N 天数据**重训练**模型（rolling window），新预测覆盖旧预测
- 模型预测的"分数"存在 `factor_values.qlib_QXX_score`（CN 是 `qlib_CQXX_score`）字段
- 每个 Q 账户取 score 最高的 Top-N 支股票持仓

**为什么叫 Q 组？** Q = Qlib。**它们是工业界标杆**，看 ML 模型是否真比手写因子或 GP 进化更强。

### IDX 类 · 基准指数（不是策略）

- **IDX1** = QQQ（纳斯达克 100 ETF）
- **IDX2** = SPY（标普 500 ETF）
- **IDX3** = 沪深 300 指数 (000300.SH)

这些不是策略账户，是**对标基准**。仪表盘的 Equity Curve 把它们叠加在所有策略曲线上，所以你能一眼看出"我的策略今年赢了多少 / 输了多少 SPY"。任何一个策略如果**长期跑不赢 SPY**，那它就没有存在的意义（不如直接买 ETF 躺平）。

## 4. 一天里发生了什么？（典型 cron 节奏）

```
盘前
├── 价格数据更新（akshare/yfinance 拉昨日收盘 + 最新日内 1d/1h K 线）
├── 因子重算（Alpha158 全量、GP 表达式、Qlib 模型推理）
└── 信号生成（每个账户根据自己的因子打分 → 决定换仓清单）

盘中（每 15-30 分钟一次 cron tick）
├── 拉实时报价（yfinance fast_info / akshare 1m bar）
├── 检查信号变化、止盈止损触发
├── 模拟撮合下单（按 moomoo AU 费率算手续费）
└── mark-to-market（用最新价重估每个账户的 equity）

盘后
├── 写入当日快照 → accounts 表（一行 = 一个账户在一个时刻的现金/持仓总值）
├── 写入持仓快照 → snapshots 表（hover tooltip 用）
└── 23:00 UTC：Qlib 模型 rolling retrain（重训 10 个 Q 账户的 ML 模型）

实时
└── lifecycle / 风控 / 错误事件 → events 表（dashboard 实时滚动显示）
```

整个过程**完全自动化**。dashboard 本身**不参与**任何决策、不写任何数据，只是把以上过程的结果可视化出来。

## 5. 账户的"生命周期"

- **诞生**：在 `account_meta` 表里 INSERT 一行，记 `created_at`、初始资金、所属组别、策略名、描述。然后 cron 开始为这个账户计算因子、生成信号、记录每日 equity。
- **活着**：每天有新 trade、新 snapshot、新 equity 数据点。仪表盘的 "Active" tab 列出所有活着的账户，按收益排序。
- **退役**：在 `account_meta.status = 'retired'`，写入 `retired_at` 时间戳和 `retire_reason`（墓志铭）。系统**停止**为它生成新信号、停止下单，但**保留所有历史数据**供回放和对照。仪表盘的 "Retired" tab 显示墓碑墙。
- **复活**：理论上把 status 改回 active 就能复活（quant-trading 有 `retire_account / unretire_account` 命令）。但通常退役后就让它躺着 —— 它的历史本身就是研究素材。

退役的常见原因：
1. **因子重复**（同一个集群里多个账户进化出高度相关的 alpha） → 留一个、其余进墓碑
2. **长期跑输基准**（夏普 < 0、收益不及 SPY） → 证明策略无效
3. **回撤过大**（最大回撤超过预设阈值） → 风控触发
4. **测试账户**（如 `C01 测试策略`）跑完研究目的后归档

## 6. 如果你想了解更多（按角色推荐）

- **完全外行** → 在仪表盘点开任何一个 A 组账户，看它的因子卡片，每个因子都有公式 + 中文动机
- **量化新人** → 对比 A 组（人写的）/ B 组（GP 挖的）/ Q 组（ML 学的）三个曲线，看哪一类长期更强
- **研究员** → 看 B 组的墓碑墙 + 退役原因，这是真实的因子去重 / 探索 / 失败记录
- **交易员** → 对比 US 和 CN 同名策略（A01 vs CA01），看跨市场可移植性
- **想验证想法** → 用回测页面（Backtest Analysis tab）选一个账户、设一个时间窗口，重放历史

## 7. 这套系统不是什么

- **不是**真钱交易系统。所有撮合都在内存里、用历史/实时价格模拟。
- **不是**做高频。最快的 cron tick 也是分钟级，因子计算用日线/小时线为主。
- **不是**多空对冲基金。所有账户都是**纯多头**（只买不空），更像散户级长线 alpha 选股。
- **不是**自动化的研究平台。新加策略、调超参、改风控仍需要手动改代码 + 重启 cron。

但它**是**：一个真实的、长期运行的、多策略对照的、有完整数据留痕的、可复现的纸面交易实验场。每天的 PnL 都是真实市场行情驱动的。哪个策略今天赚了哪一笔、为什么买、为什么卖，全部可追溯。

---

## 8. 本地运行 / 部署

> 这个仓库是**前端 dashboard**。它读取 [quant-trading](https://github.com/nullptr0807/quant-trading) 写出的 SQLite 数据库 (`trading.db`) 并通过 FastAPI 暴露给浏览器。

### 技术栈

- **后端**: Python 3.11+, FastAPI, uvicorn, SQLite (只读)
- **前端**: 纯 vanilla JS（无框架、无构建步骤）+ KaTeX (公式渲染)
- **数据源**: 通过 `core/price_cache.py` 适配器复用 `~/quant-trading/trading.db`（单一数据源）

### 目录结构

```
trading-dashboard/
├── server.py              # FastAPI 入口
├── api/                   # 路由模块（trade, factors, backtest, events, explore, intro）
├── core/                  # 业务逻辑（db 访问、回测引擎、因子公式渲染、universe）
├── static/
│   ├── index.html
│   ├── css/style.css
│   ├── js/                # 前端模块（app/trade/factors/backtest/events/explore/i18n）
│   └── explore/           # 研究文章 markdown（中英双语）
└── scripts/prefetch_all.py
```

### 安装与启动

```bash
git clone https://github.com/nullptr0807/trading-dashboard.git
cd trading-dashboard
python3 -m venv venv && source venv/bin/activate
pip install fastapi uvicorn jinja2 python-multipart pandas numpy
# 让 dashboard 知道 quant-trading 数据库在哪
export QUANT_DB_PATH=$HOME/quant-trading/data/trading.db
uvicorn server:app --host 0.0.0.0 --port 8501
```

打开 http://localhost:8501 。如果 `trading.db` 还没数据，先去 [quant-trading](https://github.com/nullptr0807/quant-trading) 跑一次 `python main.py --once`。

### 生产部署（参考）

仓库主作者在 Azure VM 上的部署：
- **nginx** 反向代理 443（自签 SSL）→ uvicorn :8501
- HTTP 80 自动 301 → HTTPS
- 配置文件 `/etc/nginx/sites-available/trading-dashboard`

### 开发说明

- 前端**没有 build 步骤**，改完 `static/js/*.js` 直接刷新浏览器即可
- 所有 API 路由都在 `api/`，遵循 `/api/<feature>/<action>` 命名
- 中英文切换走 `static/js/i18n.js` —— 所有文案都有 `data-i18n` key
- 添加新研究文章：在 `static/explore/<slug>/` 放 `article.zh.md` + `article.en.md` + 任意配图，再 append 到 `static/explore/index.json`

### License

MIT — 仅供学习研究。**这不是投资建议**。

