# Cyber Quant Arena · Trading Dashboard

> 一个为 `~/quant-trading` 模拟交易系统量身打造的可视化前台 —— 多策略账户竞技场、实时行情、历史回放和因子可解释性。

整套系统是 **dashboard 前台** + **quant-trading 引擎后台** 的两件套。本仓库只负责"看"，不负责"交易"：所有的下单、轮询、估值都发生在 `~/quant-trading/`，dashboard 只是一个对其 SQLite 数据库的**只读**视图（`PRAGMA query_only = ON`），加上一些聚合、可解释性、回测重放层。

---

## 1. 设计动机

### 为什么不直接用 Streamlit / Grafana?

- **Streamlit** 每次交互都重跑脚本，对账户数 ≥ 20、每个账户都要画 equity 曲线 + 持仓表的场景，刷新一次 5–10 秒。
- **Grafana** 强在时序 panel，弱在我们要的"账户卡片 + 持仓 accordion + 回测画布 + 因子公式渲染"那种**异构组件 dashboard**。
- 我们要的是 **macOS-style 应用** 的体验：原生切换、毫秒级响应、苹果浅色磨砂玻璃主题、KaTeX 渲染因子公式、Lightweight Charts 画 K 线，没有任何前端框架（vanilla JS + ES modules + SPA hash router）。

### 为什么前后端分离 + 单一数据源?

- 量化引擎（`~/quant-trading/`）是**写者**：cron 每天跑因子计算、生成信号、估值、写入 `trading.db`。
- Dashboard 是**读者**：只 SELECT，不 INSERT/UPDATE。这避免了"画面刷新把脏数据回写库"的整类 bug。
- **数据库 = SQLite 单文件 = 单一事实源**。两个项目共享 `~/quant-trading/data/trading.db`，dashboard 通过 `core/db.py` 用 aiosqlite 异步读取，价格历史则通过 `core/price_cache.py` 反向 import quant-trading 的 `DataStore + DataFetcher`，保证缓存命中、口径完全一致（同一种复权、同一种 ticker 标准化）。

---

## 2. 顶层架构

```
┌────────────────────────────────────────────────────────────┐
│                        Browser (SPA)                       │
│  static/index.html  +  hash router  +  vanilla components  │
│           │                                                │
│           ▼                                                │
│   /api/trade  /api/factors  /api/backtest  /api/events  /api/intro
└──────────────────────────┬─────────────────────────────────┘
                           │ FastAPI (uvicorn)
                           ▼
┌────────────────────────────────────────────────────────────┐
│                     server.py  (~20 行)                    │
│             4 个 router + StaticFiles mount                │
└──────────────────────────┬─────────────────────────────────┘
                           │ aiosqlite (read-only)
                           ▼
            ~/quant-trading/data/trading.db   ← single source of truth
                           ▲
                           │ writes (cron, scripts)
                           │
        ┌──────────────────┴───────────────────┐
        │      ~/quant-trading  (engine)       │
        │   factors / accounts / trading /     │
        │   data fetcher / qlib integration    │
        └──────────────────────────────────────┘
```

部署：Azure VM，**nginx** 在 443 (self-signed SSL) 反向代理 → uvicorn:8501，HTTP 80 强制 301 到 HTTPS。

---

## 3. 后端模块（`api/` + `core/`）

### `server.py` — 入口
20 行。挂 4 个 router，挂 `/static`，根路径返回 `index.html`。无 docs / redoc / openapi（最小化攻击面）。

### `core/db.py` — 异步只读 DB 网关
- aiosqlite + `Row` 工厂 → dict 列表
- 每个连接强制 `PRAGMA query_only = ON`
- 整个 dashboard 没有任何 INSERT / UPDATE / DELETE — 任何"修改"都必须通过 quant-trading 流回引擎

### `core/universe.py` — 标的宇宙缓存
- 拉取 NASDAQ + S&P 500 列表（nasdaqtrader.com 官方文件 + Wikipedia），24h TTL，落盘 `data/universe/universe.json`
- 用于回测页面的 universe size 选择
- 注：**实盘**的 Russell 1000 universe 在 `~/quant-trading/config/settings.py`，dashboard 不参与

### `core/price_cache.py` — 价格历史适配器
- 历史教训：原本 dashboard 自己 fork 了一份 parquet 价格缓存，导致两边数据不一致、双写竞争
- **现在**：所有 `get_history()` 调用全部转发到 quant-trading 的 `DataStore + DataFetcher`，写入同一个 `trading.db.prices` 表
- 公开 API 保持不变（`get_history`, `estimate_fetch_cost`）方便回测引擎复用
- 旧 parquet 已归档到 `data/prices.deprecated_parquet/`

### `core/benchmarks.py` — 基准线获取
- US: QQQ + SPY (account_id `IDX1` / `IDX2`)，CN: 沪深 300 (`IDX3`)
- yfinance 拉小时线 → 落盘 `data/benchmarks/{ticker}_{interval}.json`，15 分钟 TTL + 内存缓存
- 提供 `rebased_curve()` 把基准曲线归一到策略起点（用户能直接对比 alpha）

### `core/backtest_engine.py` — 历史回放引擎
- "Qlib-style" 真实回放：抓 OHLCV → 每日重算因子 → 信号 → 加成本模型 (moomoo AU 费率) → mark-to-market
- 复用 quant-trading 的 `FactorEngine / SignalGenerator / GPSignalGenerator / TradingEngine` —— **不是**重新实现，避免 sim/live 脱节
- 任务模型：`new_job() → asyncio.create_task → run_backtest_job(job_id, ...)`，前端轮询 `/api/backtest/job/{id}`
- Q 账户（Qlib 模型）单独短路：在每日 checkpoint 覆盖足够之前禁止回放，错误信息更清晰

### `core/factor_formulas.py` / `factor_formulas_en.py` — 因子可解释性
- Alpha158（A 组）的每个因子：`FACTOR_FORMULAS`（Python 表达式）/ `FACTOR_LATEX`（KaTeX 公式）/ `FACTOR_EXPLANATIONS`（中文动机）
- 中英双语：`*_EN.py` 提供英文版
- 还有 `FEATURE_COLS`（GP 用的 13 个原子特征：`o_c, h_c, l_c, v_vma20, ma_5/10/20, std_5/10/20, ret_1/5/10`），物理意义都标注
- 设计动机：用户（量化研究者）问"这个因子是什么意思"时，前端能直接渲染 LaTeX 公式 + 中文/英文解释，不用翻代码

### `core/gp_explain.py` — GP 树解释器
- gplearn 进化出来的因子是嵌套表达式（如 `mul(div(X3, X7), sub(X1, X2))`），人类不可读
- 自己写了一个递归下降 parser，把字符串解析成 AST
- 然后从 AST 生成四段解释：
  - **intuition** — 表达式在算什么
  - **motivation** — 设计/交易动机
  - **alpha_source** — 可能的超额收益来源（异象 grounding）
  - **warnings** — 膨胀/冗余警告（gplearn 经常进化出 `mul(X1, 1.0)` 之类的废话）
- 支持中英双语

### `api/trade.py` — 交易/账户聚合 ★ 最大、最复杂
**~537 行**。负责 dashboard 主页的所有数据：

- `GET /api/trade/summary?market=US|CN`
  返回该市场所有账户的最新快照：name / cash / equity / 分组（A/B/Q/IDX）/ strategy / status (active/retired)
  - **Fallback 链**：`accounts` 表（cron 写入）→ 若空，回退到 `account_state + account_meta`（保证 CN cron 还没首次跑时也有显示）
  - **distribution 字段**：每组的 best/worst/median/mean/q1/q3/win_rate + 每个账户的 pnl%（前端画分布直方图）
- `GET /api/trade/equity-curve?account=...` — 单账户 equity 时间序列
- `GET /api/trade/positions/<account>` — 当前持仓 + 入场/止盈止损
- `GET /api/trade/trades/<account>` — 历史交易记录
- `GET /api/trade/snapshots/<account>` — 每日持仓快照（hover tooltip 用）
- `GET /api/trade/ticker-names?market=CN&lang=zh` — A 股 CSI300 中英文名（lru_cache，源自 `~/quant-trading/data/cn_universe.json`）

设计要点：所有 endpoint 都用 `_validate_market()` 严格校验 market enum，避免 SQL 注入面。

### `api/factors.py` — 因子展示
- `GET /api/factors/<account>` — 该账户的因子列表
  - A 账户 → 从 Alpha158 名单查 `FACTOR_FORMULAS / LATEX / EXPLANATIONS`
  - B 账户 → 从 `~/quant-trading/factors/mined_alphas_per_account.json` 读 GP 表达式，调 `gp_explain.explain(expr, lang)` 生成解释
  - Q 账户 → 从 quant-trading 的 `accounts/qlib_strategies.py` 反向 import `QLIB_STRATEGIES`，拿模型描述
- `GET /api/factors/strategy-desc?account=...` — 策略级别的描述（多语言）

### `api/backtest.py` — 回测任务 API
- `POST /api/backtest/run` → 启动 async job，返回 `job_id`
- `GET /api/backtest/job/<id>` → 轮询进度（status / progress / message / error / result）
- `GET /api/backtest/accounts?market=US` → 可回测的账户列表（含 retired）
- `GET /api/backtest/date-range` → 智能默认（最近 90 天 + trades 表实际范围）

### `api/events.py` — 系统事件流
- 一个 SELECT。读 `events` 表（cron 和 trading 模块写入）：lifecycle / inception / 预警 / 错误等
- 支持 `after_id` 增量轮询，前端做实时滚动

### `api/intro.py` — 本仓库 README（新增）
- `GET /api/intro` → 返回 `README.md` 原文
- 前端 fetch 后用 marked.js 渲染

---

## 4. 前端模块（`static/`）

### `index.html` — 单页应用骨架
- 50 行。固定导航条 + `<main id="app">` 容器 + KaTeX/Lightweight Charts CDN
- 没有任何前端框架。加载完一次，之后所有页面切换都是 hash 路由 → 替换 `#app.innerHTML`

### `static/css/style.css` — 苹果浅色磨砂玻璃主题
- **设计语言**：参考 apple.com / Big Sur Control Center / iOS 控件
- 4 块柔和粉彩光斑（蓝/紫/粉/青，alpha 0.14–0.18）作为底层 backdrop
- `.glass-card` 用 `backdrop-filter: blur(28px) saturate(180%)` + 顶边白色高光 + 多层 box-shadow
- 主色：苹果蓝 `#0071e3` / 紫 `#ac39ff` / 粉 `#ff2d92`
- 文本：`#1d1d1f`（苹果官方主文字色）/ `rgba(0,0,0,0.58)` 副文字
- 1372 行 CSS，覆盖了 nav / hero / cards / accordion / modal / drawer / tooltip / 墓碑墙等 50+ 组件

### `static/js/app.js` — SPA 路由 + 全局状态
- 全局 `state.market`（'US' | 'CN'），从 URL `?market=` 初始化
- `api(path)` 统一封装：自动加 `?lang=` 和 `&market=` 参数
- Hash router：`#/trade` / `#/backtest` / `#/intro`，路由变化时淡入淡出
- 通用工具：`formatMoney / formatPercent / formatTicker / animateNumber`
- Ticker name 缓存按市场分桶，避免重复请求

### `static/js/i18n.js` — 国际化字典
- 463 行 `I18N_DICT.en` / `I18N_DICT.zh`
- `t(key, vars)` 帮助函数，支持 `{n}` 占位符插值
- 语言选择持久化到 `localStorage`，切换时调用所有渲染器重绘

### `static/js/trade.js` — 主页（700 行）
- Hero：总资产 + 今日 PnL（动画数字递增）
- Distribution 卡：分布统计 + 12 桶直方图（按策略分层堆叠，hover 弹账户列表）
- Equity Curve：所有账户 + 基准线叠加，Lightweight Charts
- Accounts Overview：手风琴 / 账户行 → 展开后含因子卡 + equity 子图 + 持仓表 + 交易历史
- Tombstone Wall：退役账户的"墓碑"（保留深色，致敬主题）

### `static/js/components.js` — 共享组件（750 行）
- `createPositionsTable()` — 持仓表
- `createEquityChart()` — 单账户 equity（含 hover tooltip 显示该时刻持仓快照）
- `createDrawer()` — 抽屉式详情面板
- `createSnapshotTip / TradeTip` — chart hover 浮窗

### `static/js/backtest.js` — 回测页（795 行）
- 账户多选 + 日期范围 + universe size + 初始资金
- 提交后轮询 job → 显示进度条 + 实时消息
- 完成后画总图 + 每账户子图 + 交易明细
- 同样支持 chart hover tooltip

### `static/js/events.js` — 事件流轮询
- 156 行。每 5s 拉 `/api/events?after_id=last_id`
- 增量插入到顶部，斑马纹（白 / 浅蓝），新事件淡入

---

## 5. 数据流（典型一次刷新）

```
1. 浏览器加载 index.html → app.js → i18n.js → trade.js / backtest.js
2. trade.js 启动 → fetch('/api/trade/summary?market=US&lang=en')
3. FastAPI 路由到 api/trade.py:summary()
4. core/db.py 用 aiosqlite (PRAGMA query_only) 读 ~/quant-trading/data/trading.db
5. JSON 返回 → 前端拼 hero / cards / charts
6. Lightweight Charts 渲染 equity curve（数据来自 /api/trade/equity-curve）
7. hover 触发 chart.subscribeCrosshairMove → 查 snapByTs[ts] → 显示持仓 tooltip
```

---

## 6. 设计原则总结

| 原则 | 体现 |
|---|---|
| **单一事实源** | dashboard 不持有任何主数据，全部从 quant-trading 的 SQLite 读 |
| **只读边界** | `PRAGMA query_only` 在 connection level 强制 |
| **同口径** | 价格、ticker 标准化、复权全走 quant-trading 的 `DataStore` |
| **降级链** | `accounts` 空 → `account_state + account_meta`，保证 dashboard 永远有东西显示 |
| **可解释性优先** | 每个因子都有公式 + 中英文解释 + GP 表达式自动解释器 |
| **零前端框架** | vanilla JS + hash router，加载快，调试简单 |
| **苹果浅色磨砂玻璃** | 一致视觉语言，CSS 变量驱动主题切换可能性 |
| **市场可扩展** | US/CN 全部走同一套 endpoint，新增市场只要在 `_validate_market` 加 enum + benchmarks 加映射 |

---

## 7. 仓库布局

```
trading-dashboard/
├── server.py              # FastAPI 入口（20 行）
├── api/                   # HTTP 路由层
│   ├── trade.py          # 账户/equity/持仓/交易聚合 ★ 537 行
│   ├── factors.py        # 因子可解释性 347 行
│   ├── backtest.py       # 回测任务 API 176 行
│   ├── events.py         # 事件流 45 行
│   └── intro.py          # README API（新）
├── core/                  # 业务逻辑层
│   ├── db.py             # 异步只读 DB 网关
│   ├── universe.py       # NASDAQ+S&P universe 缓存
│   ├── price_cache.py    # 价格历史适配器（→ quant-trading）
│   ├── benchmarks.py     # QQQ/SPY/CSI300 基准线
│   ├── backtest_engine.py # Qlib-style 历史回放
│   ├── factor_formulas.py # Alpha158 公式 + 中文解释
│   ├── factor_formulas_en.py # 英文版
│   └── gp_explain.py     # GP 表达式 → 自然语言
├── static/
│   ├── index.html
│   ├── css/style.css     # 苹果浅色磨砂玻璃 1372 行
│   └── js/
│       ├── app.js        # SPA 路由 + 全局状态
│       ├── i18n.js       # 中英双语字典
│       ├── trade.js      # 主页
│       ├── components.js # 共享组件
│       ├── backtest.js   # 回测页
│       └── events.js     # 事件流
├── scripts/prefetch_all.py # 批量预拉价格
├── docs/                  # 旧的 design.md / implementation.md（已 .gitignore）
└── README.md             # 本文件
```

---

## 8. 部署

- **Azure VM** Ubuntu，systemd 不用，直接 `cd ~/trading-dashboard && source venv/bin/activate && uvicorn server:app --port 8501`
- **nginx** `/etc/nginx/sites-available/trading-dashboard`：443 (self-signed SSL) reverse proxy → 8501，80 → 301 → 443
- **Azure VM 端口**：22 / 80 / 443 对外开放
- **Git**：已建本地仓库，每次 UI / 后端改动都独立 commit（见 `git log`）

---

## 9. 致谢与边界

- 真正的"量化"工作（因子挖掘、模型训练、信号生成、风控、模拟撮合）全部在 `~/quant-trading/`。本仓库只是它的"显示器"。
- 但**可解释性层**（公式、GP 树解释、墓碑墙、墓志铭）是 dashboard 独有的，引擎不关心这些。
- 苹果浅色磨砂玻璃主题完全是为了好看 —— 但好看也是生产力。

---

# Part II · 量化交易系统本身

> 上面讲的是"显示器"，下面这部分讲"显示器后面那台机器在做什么"。
> 写给完全没接触过量化的人。

## 10. 这个系统在干什么？（一分钟版本）

我们**没有真的在交易**。这是一个**纸面交易竞技场**：

- 一开始，给每个虚拟账户发一笔本金（美股 $10,000，A 股 ¥100,000）
- 每个账户跑一个**不同的策略**（一种炒股的"信仰"，比如"追涨" vs "抄底"）
- 系统每天定时（cron）拉取真实的市场行情，让每个策略**自己决定**买什么、卖什么、各买多少
- 我们**不动手**，只看着这些虚拟账户的钱涨涨跌跌
- 跑得不好的账户会被**退役**（变成墓碑），证明这个策略在真实市场里活不下去

为什么要这么做？—— 在拿真钱去市场之前，先用**多个互相竞争的策略**在历史和实时数据上做对照实验，看哪一种"信仰"长期有效。这是经典量化研究的"赛马"思路：与其相信一个策略，不如同时养 20 个，让市场告诉你哪个能活。

## 11. 两个市场：US（美股）和 CN（A 股）

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

## 12. 三大类账户：A / B / Q

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

## 13. 一天里发生了什么？（典型 cron 节奏）

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

## 14. 账户的"生命周期"

- **诞生**：在 `account_meta` 表里 INSERT 一行，记 `created_at`、初始资金、所属组别、策略名、描述。然后 cron 开始为这个账户计算因子、生成信号、记录每日 equity。
- **活着**：每天有新 trade、新 snapshot、新 equity 数据点。仪表盘的 "Active" tab 列出所有活着的账户，按收益排序。
- **退役**：在 `account_meta.status = 'retired'`，写入 `retired_at` 时间戳和 `retire_reason`（墓志铭）。系统**停止**为它生成新信号、停止下单，但**保留所有历史数据**供回放和对照。仪表盘的 "Retired" tab 显示墓碑墙。
- **复活**：理论上把 status 改回 active 就能复活（quant-trading 有 `retire_account / unretire_account` 命令）。但通常退役后就让它躺着 —— 它的历史本身就是研究素材。

退役的常见原因：
1. **因子重复**（同一个集群里多个账户进化出高度相关的 alpha） → 留一个、其余进墓碑
2. **长期跑输基准**（夏普 < 0、收益不及 SPY） → 证明策略无效
3. **回撤过大**（最大回撤超过预设阈值） → 风控触发
4. **测试账户**（如 `C01 测试策略`）跑完研究目的后归档

## 15. 如果你想了解更多（按角色推荐）

- **完全外行** → 在仪表盘点开任何一个 A 组账户，看它的因子卡片，每个因子都有公式 + 中文动机
- **量化新人** → 对比 A 组（人写的）/ B 组（GP 挖的）/ Q 组（ML 学的）三个曲线，看哪一类长期更强
- **研究员** → 看 B 组的墓碑墙 + 退役原因，这是真实的因子去重 / 探索 / 失败记录
- **交易员** → 对比 US 和 CN 同名策略（A01 vs CA01），看跨市场可移植性
- **想验证想法** → 用回测页面（Backtest Analysis tab）选一个账户、设一个时间窗口，重放历史

## 16. 这套系统不是什么

- **不是**真钱交易系统。所有撮合都在内存里、用历史/实时价格模拟。
- **不是**做高频。最快的 cron tick 也是分钟级，因子计算用日线/小时线为主。
- **不是**多空对冲基金。所有账户都是**纯多头**（只买不空），更像散户级长线 alpha 选股。
- **不是**自动化的研究平台。新加策略、调超参、改风控仍需要手动改代码 + 重启 cron。

但它**是**：一个真实的、长期运行的、多策略对照的、有完整数据留痕的、可复现的纸面交易实验场。每天的 PnL 都是真实市场行情驱动的。哪个策略今天赚了哪一笔、为什么买、为什么卖，全部可追溯。

