# Trading Dashboard — 设计文档

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 构建一个独立的、炫酷的量化交易 Web Dashboard，实时展示 20 个虚拟账户的交易数据，支持回测功能。

**Architecture:** FastAPI 后端 + 纯前端 (Vanilla JS + Chart.js/Lightweight Charts)，单页应用。后端直接读取 quant-trading 的 SQLite DB（只读）。前端采用 Apple 官网 / Hermes Agent 风格，深色主题，毛玻璃效果，流畅动画。

**Tech Stack:**
- Backend: Python FastAPI, SQLite (read-only from ~/quant-trading/data/trading.db)
- Frontend: Vanilla JS + CSS (no React/Vue — keep it simple), TradingView Lightweight Charts for equity curves, KaTeX for LaTeX rendering
- Styling: CSS custom properties, backdrop-filter glassmorphism, CSS animations

---

## 项目结构

```
trading-dashboard/
├── server.py                  # FastAPI 主入口
├── api/
│   ├── __init__.py
│   ├── trade.py               # /trade 页面 API
│   ├── backtest.py            # /backtest 页面 API
│   └── factors.py             # 因子公式 & 解释 API
├── static/
│   ├── css/
│   │   └── style.css          # 全局样式（深色主题、毛玻璃、动画）
│   ├── js/
│   │   ├── app.js             # 路由 & 全局状态
│   │   ├── trade.js           # /trade 页面逻辑
│   │   ├── backtest.js        # /backtest 页面逻辑
│   │   └── components.js      # 可复用 UI 组件
│   └── index.html             # SPA 入口
├── core/
│   ├── __init__.py
│   ├── db.py                  # SQLite 读取层
│   ├── factor_formulas.py     # 因子公式 + LaTeX + 物理/数学解释
│   └── backtest_engine.py     # 回测引擎
├── docs/
│   └── plans/
│       ├── design.md          # 本文件
│       └── implementation.md  # 实施计划
└── requirements.txt
```

## 页面设计

### /trade — 实时交易报告

**布局 (从上到下):**

1. **Hero Header** — 总权益、总盈亏、A/B 组对比，大字体 + 渐变动画
2. **Equity Curves Panel** — 20 条权益曲线叠加
   - Hover 某条曲线时：该曲线高亮，其余 80% 透明度
   - 高亮时下方弹出浮动面板，显示该账户的：
     - 当前持仓（股票、数量、市值、盈亏%）
     - 买卖点标记在曲线上
     - 最近交易历史
3. **Account Cards Grid** — 20 个账户卡片，3-4 列
   - 每张卡片：账户名、策略名、权益、盈亏%、迷你曲线
   - 点击展开详情
4. **Factor Analysis Panel** — 每个账户的因子
   - LaTeX 渲染的数学公式
   - 物理/数学角度的解释文字
   - A 组：Alpha158 因子（固定公式）
   - B 组：GP 进化因子（gplearn 表达式转数学符号）

### /backtest — 回测系统

**布局:**

1. **配置面板** (左侧)
   - 账户选择（单选/多选）
   - 初始资金输入
   - 日期范围选择
   - 市场选择（US/China）
2. **结果面板** (右侧)
   - 回测权益曲线
   - 收益统计：总收益、年化、最大回撤、Sharpe、Sortino
   - 交易明细表
   - 对比模式：多账户叠加

## 设计风格

- **色彩：** 深色背景 (#0a0a0f → #1a1a2e 渐变)，强调色渐变 (#00d4ff → #7b2ff7)
- **字体：** SF Pro Display / Inter，数字用等宽
- **卡片：** 毛玻璃效果 (backdrop-filter: blur(20px))，微妙边框
- **动画：** 页面加载渐入，数字跳动，曲线绘制动画，hover 过渡 0.3s ease
- **图表：** 深色主题，荧光色曲线，渐变填充

## 数据源

只读连接 `~/quant-trading/data/trading.db`:
- `accounts` — 权益快照时间序列 (200 rows, ~10 per account)
- `account_state` — 当前现金余额 (20 rows)
- `account_meta` — 策略名称、因子列表 (20 rows)
- `positions` — 当前持仓 (105 rows)
- `positions_history` — 持仓历史快照 (3990 rows)
- `trades` — 交易记录 (310 rows)
- `factor_values` — 因子值 (89556 rows)
- `market_returns` — 市场收益 (20 rows)

## TODO

- [ ] Phase 1: 后端骨架 + API
- [ ] Phase 2: 前端 /trade 页面
- [ ] Phase 3: 因子公式渲染
- [ ] Phase 4: /backtest 页面
- [ ] Phase 5: 动画 & 打磨

## COMPLETED

(无)
