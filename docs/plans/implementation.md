# Trading Dashboard — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a standalone, visually stunning trading dashboard web app.

**Architecture:** FastAPI backend serving static files + JSON APIs. Frontend is vanilla JS SPA with TradingView Lightweight Charts and KaTeX. Reads SQLite DB from ~/quant-trading/data/trading.db (read-only).

**Tech Stack:** Python 3.12, FastAPI, uvicorn, aiosqlite; Vanilla JS, Lightweight Charts, KaTeX (CDN)

---

## Task 1: Project Setup & Dependencies

**Objective:** Create project skeleton, virtualenv, install deps, verify server starts.

**Files:**
- Create: `~/trading-dashboard/requirements.txt`
- Create: `~/trading-dashboard/server.py`
- Create: `~/trading-dashboard/core/__init__.py`
- Create: `~/trading-dashboard/core/db.py`
- Create: `~/trading-dashboard/api/__init__.py`
- Create: `~/trading-dashboard/static/index.html` (minimal placeholder)

**Steps:**

1. Create `requirements.txt`:
```
fastapi==0.115.0
uvicorn[standard]==0.30.0
aiosqlite==0.20.0
```

2. Create virtualenv and install:
```bash
cd ~/trading-dashboard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Create `core/db.py` — async SQLite read-only wrapper:
```python
"""Read-only async access to trading.db."""
import aiosqlite
from pathlib import Path

DB_PATH = Path.home() / "quant-trading" / "data" / "trading.db"

async def get_db():
    db = await aiosqlite.connect(str(DB_PATH), uri=True)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA query_only = ON")
    return db

async def fetch_all(query: str, params=()) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        await db.close()

async def fetch_one(query: str, params=()) -> dict | None:
    rows = await fetch_all(query, params)
    return rows[0] if rows else None
```

4. Create `server.py`:
```python
"""Trading Dashboard — FastAPI server."""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="Trading Dashboard")

# API routers (added in later tasks)
# from api.trade import router as trade_router
# app.include_router(trade_router, prefix="/api")

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")
```

5. Create minimal `static/index.html`:
```html
<!DOCTYPE html>
<html><head><title>Trading Dashboard</title></head>
<body style="background:#0a0a0f;color:#fff;font-family:sans-serif;text-align:center;padding:100px">
<h1>Trading Dashboard</h1><p>Loading...</p>
</body></html>
```

6. Verify: `cd ~/trading-dashboard && source venv/bin/activate && uvicorn server:app --port 8501` — should see "Uvicorn running"

**Commit:** `git init && git add -A && git commit -m "feat: project skeleton with FastAPI"`

---

## Task 2: Trade API Endpoints

**Objective:** Build all /api/trade/* endpoints returning JSON data.

**Files:**
- Create: `~/trading-dashboard/api/trade.py`
- Modify: `~/trading-dashboard/server.py` (register router)

**Endpoints:**

1. `GET /api/trade/summary` — overall stats (total equity, PnL, A vs B group)
2. `GET /api/trade/accounts` — all 20 accounts with equity, PnL%, strategy info
3. `GET /api/trade/equity-curves` — time series for all accounts (for chart)
4. `GET /api/trade/account/{id}` — single account detail: positions, trades, equity history
5. `GET /api/trade/positions/{id}` — current positions for account
6. `GET /api/trade/trades?account={id}&limit=50` — trade history

**Implementation for `api/trade.py`:**
```python
from fastapi import APIRouter
from core.db import fetch_all, fetch_one

router = APIRouter(prefix="/api/trade", tags=["trade"])

@router.get("/summary")
async def summary():
    accounts = await fetch_all("""
        SELECT a.account, a.cash, a.initial_cash,
               COALESCE(p.mv, 0) as positions_value
        FROM account_state a
        LEFT JOIN (SELECT account, SUM(shares*current_price) as mv FROM positions GROUP BY account) p
        ON a.account = p.account
    """)
    total_equity = sum(r['cash'] + r['positions_value'] for r in accounts)
    total_initial = sum(r['initial_cash'] for r in accounts)
    a_equity = sum(r['cash'] + r['positions_value'] for r in accounts if r['account'].startswith('A'))
    b_equity = sum(r['cash'] + r['positions_value'] for r in accounts if r['account'].startswith('B'))
    return {
        "total_equity": total_equity,
        "total_initial": total_initial,
        "total_pnl": total_equity - total_initial,
        "total_pnl_pct": (total_equity - total_initial) / total_initial * 100,
        "a_group": {"equity": a_equity, "pnl_pct": (a_equity - 100000) / 100000 * 100},
        "b_group": {"equity": b_equity, "pnl_pct": (b_equity - 100000) / 100000 * 100},
        "account_count": len(accounts),
    }

@router.get("/accounts")
async def accounts():
    rows = await fetch_all("""
        SELECT a.account, a.cash, a.initial_cash,
               COALESCE(p.mv, 0) as positions_value,
               m.strategy_name, m.description, m.`group`, m.factors
        FROM account_state a
        LEFT JOIN (SELECT account, SUM(shares*current_price) as mv FROM positions GROUP BY account) p ON a.account = p.account
        LEFT JOIN account_meta m ON a.account = m.account_id
        ORDER BY (a.cash + COALESCE(p.mv, 0)) DESC
    """)
    result = []
    for r in rows:
        equity = r['cash'] + r['positions_value']
        result.append({
            "id": r['account'],
            "name": r['strategy_name'],
            "description": r['description'],
            "group": r['group'],
            "factors": r['factors'].split(',') if r['factors'] else [],
            "equity": equity,
            "cash": r['cash'],
            "initial_cash": r['initial_cash'],
            "pnl": equity - r['initial_cash'],
            "pnl_pct": (equity - r['initial_cash']) / r['initial_cash'] * 100,
        })
    return result

@router.get("/equity-curves")
async def equity_curves():
    rows = await fetch_all("SELECT name, equity, timestamp FROM accounts ORDER BY name, timestamp")
    curves = {}
    for r in rows:
        curves.setdefault(r['name'], []).append({"t": r['timestamp'], "v": r['equity']})
    return curves

@router.get("/account/{account_id}")
async def account_detail(account_id: str):
    meta = await fetch_one("SELECT * FROM account_meta WHERE account_id = ?", (account_id,))
    state = await fetch_one("SELECT * FROM account_state WHERE account = ?", (account_id,))
    positions = await fetch_all("""
        SELECT ticker, shares, avg_cost, current_price,
               shares*current_price as market_value,
               (current_price - avg_cost)/avg_cost*100 as pnl_pct
        FROM positions WHERE account = ? ORDER BY shares*current_price DESC
    """, (account_id,))
    trades = await fetch_all(
        "SELECT * FROM trades WHERE account = ? ORDER BY timestamp DESC LIMIT 50",
        (account_id,)
    )
    equity = await fetch_all(
        "SELECT equity, timestamp FROM accounts WHERE name = ? ORDER BY timestamp",
        (account_id,)
    )
    return {"meta": meta, "state": state, "positions": positions, "trades": trades, "equity": equity}

@router.get("/recent-trades")
async def recent_trades(limit: int = 20):
    return await fetch_all("SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,))
```

Register in server.py — add `from api.trade import router as trade_router` and `app.include_router(trade_router)`.

**Verify:** `curl http://localhost:8501/api/trade/summary` returns JSON with total_equity ~$203,517.

**Commit:** `git add -A && git commit -m "feat: trade API endpoints"`

---

## Task 3: Factor Formulas API

**Objective:** Build factor formula endpoint with LaTeX + explanations.

**Files:**
- Create: `~/trading-dashboard/core/factor_formulas.py`
- Create: `~/trading-dashboard/api/factors.py`
- Modify: `~/trading-dashboard/server.py` (register router)

**Core logic in `core/factor_formulas.py`:**

Contains three dicts:
1. `FACTOR_LATEX` — LaTeX math notation for each Alpha158 factor
2. `FACTOR_EXPLANATIONS` — physics/math perspective explanations (Chinese)
3. `GP_FUNC_MATH` / `GP_VAR_MATH` — GP expression converter (copy from main.py)
4. `gp_expr_to_math()` and `gp_expr_to_latex()` functions

Example entries:
```python
FACTOR_LATEX = {
    "ROC_5": r"\text{ROC}_5 = \frac{P_t}{P_{t-5}} - 1",
    "RSI_14": r"\text{RSI}_{14} = 100 - \frac{100}{1 + \frac{\text{SMA}(\Delta^+, 14)}{\text{SMA}(\Delta^-, 14)}}",
    "BBPOS_5": r"\text{BB}_5 = \frac{P_t - \mu_5}{2\sigma_5}",
    # ... all 30 Alpha158 factors
}

FACTOR_EXPLANATIONS = {
    "ROC_5": "5日收益率动量。类比物理中的速度概念 v = Δx/Δt。价格的一阶导数，衡量趋势强度。当 ROC > 0 时表示价格处于上升趋势，类似于信号处理中正的频率分量。",
    "RSI_14": "相对强弱指标，本质是涨跌幅的归一化。类比 Weber-Fechner 定律，人类对刺激的感知是对数尺度的——RSI 将绝对涨跌转换为 [0,100] 的感知尺度。在信号处理中相当于功率谱密度的归一化。",
    # ... etc
}
```

**API endpoint `GET /api/factors/{account_id}`:**
- For A-group: return LaTeX + explanation from static dicts
- For B-group: read GP expressions from DB/files, convert to LaTeX via gp_expr_to_latex()

**Commit:** `git add -A && git commit -m "feat: factor formulas API with LaTeX and explanations"`

---

## Task 4: Frontend SPA Shell & Styling

**Objective:** Build the full HTML/CSS shell — navigation, routing, dark theme, glassmorphism.

**Files:**
- Rewrite: `~/trading-dashboard/static/index.html`
- Create: `~/trading-dashboard/static/css/style.css`
- Create: `~/trading-dashboard/static/js/app.js`

**index.html** loads:
- KaTeX CSS+JS from CDN
- Lightweight Charts from CDN
- Local CSS and JS

**style.css** implements:
- CSS custom properties for theme colors
- Dark gradient background
- Glassmorphism card class (.glass-card)
- Number animation (@keyframes countUp)
- Page transition animations
- Responsive grid (auto-fit, minmax)
- Typography (SF Pro / Inter stack)
- Glow effects for accents

**app.js** implements:
- Simple hash-based router (#/trade, #/backtest)
- Page loading with fade transitions
- Utility functions (formatCurrency, formatPercent, animateNumber)

**Verify:** Open browser, see dark themed page with navigation working.

**Commit:** `git add -A && git commit -m "feat: SPA shell with dark theme and glassmorphism"`

---

## Task 5: /trade Page — Hero Header & Summary

**Objective:** Build the top hero section showing total equity, PnL, group comparison.

**Files:**
- Create: `~/trading-dashboard/static/js/trade.js`
- Modify: `~/trading-dashboard/static/js/app.js` (wire up route)

**Hero section shows:**
- Total equity (big animated number)
- Total PnL $ and %
- A vs B group comparison bar
- "Live" indicator dot

Fetches from `/api/trade/summary`.

**Commit:** `git add -A && git commit -m "feat: trade page hero header with summary stats"`

---

## Task 6: /trade Page — Equity Curves Chart

**Objective:** Interactive equity curves using Lightweight Charts.

**Files:**
- Modify: `~/trading-dashboard/static/js/trade.js`

**Behavior:**
- 20 lines (A-group blue shades, B-group purple shades)
- Hover highlights one line, dims others to 20% opacity
- Crosshair shows account name + equity value
- Below chart: floating detail panel appears on hover with account info

Fetches from `/api/trade/equity-curves`.

**Commit:** `git add -A && git commit -m "feat: interactive equity curves with hover highlight"`

---

## Task 7: /trade Page — Account Cards Grid

**Objective:** 20 account cards in responsive grid.

**Files:**
- Modify: `~/trading-dashboard/static/js/trade.js`
- Create: `~/trading-dashboard/static/js/components.js`

**Each card shows:**
- Account ID + strategy name (Chinese)
- Equity + PnL% with color coding
- Mini sparkline (canvas)
- Click to expand: positions table, recent trades, factor formulas (with KaTeX)

Fetches from `/api/trade/accounts` + `/api/trade/account/{id}` on expand.

**Commit:** `git add -A && git commit -m "feat: account cards grid with expandable details"`

---

## Task 8: /trade Page — Factor Display with KaTeX

**Objective:** Render factor formulas with LaTeX and explanations.

**Files:**
- Modify: `~/trading-dashboard/static/js/trade.js` or `components.js`

**In expanded card, Factor section shows:**
- Each factor name as heading
- LaTeX formula rendered by KaTeX
- Explanation text in Chinese (physics/math perspective)
- For B-group: GP tree expression → mathematical notation

Fetches from `/api/factors/{account_id}`.

**Commit:** `git add -A && git commit -m "feat: factor display with KaTeX LaTeX rendering"`

---

## Task 9: /backtest Page

**Objective:** Configurable backtest interface.

**Files:**
- Create: `~/trading-dashboard/static/js/backtest.js`
- Create: `~/trading-dashboard/api/backtest.py`
- Create: `~/trading-dashboard/core/backtest_engine.py`

**Config panel (left):**
- Account selector (checkboxes)
- Initial capital input (default $10,000)
- Date range picker (start/end)
- Market selector (US only for now)
- "Run Backtest" button

**Results panel (right):**
- Equity curve chart
- Stats table: total return, annualized, max drawdown, Sharpe, Sortino, win rate
- Trade log table (sortable)

**Backtest engine:**
- Replay trades from DB within date range
- Recalculate equity curve from initial capital
- Compute stats

**Commit:** `git add -A && git commit -m "feat: backtest page with configurable parameters"`

---

## Task 10: Polish & Animation

**Objective:** Final visual polish — loading states, animations, error handling.

**Steps:**
- Add page load skeleton screens
- Smooth number counting animation
- Chart draw animation
- Error toast notifications
- Loading spinners
- Mobile responsive tweaks
- Favicon (gradient circle)

**Commit:** `git add -A && git commit -m "feat: visual polish, animations, and error handling"`

---

## Execution Notes

- Tasks 1-3 are backend, can be done sequentially
- Tasks 4-8 are frontend /trade page, sequential
- Task 9 (/backtest) is independent of 4-8
- Task 10 is final polish after everything works

Total estimated: ~10 tasks, each 5-15 minutes for a subagent.
