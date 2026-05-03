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
