# Moomoo Live Account Module Implementation Plan

> **For Hermes:** Implement task-by-task with tests and fail-closed verification.

**Goal:** Add a user-friendly real-account page that reads account, positions, quotes, orders and deals only from Moomoo OpenD, with real ordering disabled by default and protected by multiple server-side controls.

**Architecture:** A lazy Moomoo SDK adapter in `core/` owns all OpenD access and normalizes DataFrames. A FastAPI router exposes read endpoints and guarded preview/place/cancel endpoints. A standalone vanilla-JS route renders connectivity, portfolio, policy, positions, activity and an order ticket. Audit events use a dedicated local SQLite database, never the paper ledger.

**Tech Stack:** FastAPI, Pydantic, official `moomoo-api`, vanilla JS/CSS, SQLite audit log, pytest.

---

### Task 1: Add SDK dependency and safe configuration

**Files:**
- Modify: `requirements.txt`
- Create: `core/moomoo_client.py`
- Test: `tests/test_moomoo_live_account.py`

**Steps:**
1. Add official Moomoo SDK dependency.
2. Define environment-backed immutable settings with real trading disabled by default.
3. Validate market, security firm, minimum NAV, maximum order notional and token readiness.
4. Unit-test defaults and invalid configuration.

### Task 2: Implement read-only OpenD adapter

**Files:**
- Modify: `core/moomoo_client.py`
- Test: `tests/test_moomoo_live_account.py`

**Steps:**
1. Lazily import SDK and create short-lived quote/trade contexts.
2. Normalize account, position, order, deal and snapshot DataFrames.
3. Require selected real account and USD currency.
4. Expose connection/readiness state without leaking credentials.
5. Test with fake SDK contexts.

### Task 3: Implement order safety and audit

**Files:**
- Create: `core/moomoo_audit.py`
- Create: `api/live_account.py`
- Modify: `server.py`
- Test: `tests/test_moomoo_live_account.py`

**Steps:**
1. Add overview, positions, orders, deals, quote and policy endpoints.
2. Add limit-order-only preview with Moomoo quote, cash/position and deviation validation.
3. Sign exact previews with short expiry.
4. Require server enable flag, constant-time API token, selected account, minimum NAV, unlock secret and matching preview for place/cancel.
5. Append sanitized preview/place/cancel outcomes to dedicated SQLite audit DB.
6. Verify disabled mode cannot invoke SDK mutation methods.

### Task 4: Build live-account page

**Files:**
- Create: `static/js/live_account.js`
- Modify: `static/js/app.js`
- Modify: `static/js/i18n.js`
- Modify: `static/index.html`
- Modify: `static/css/style.css`

**Steps:**
1. Add `#/live-account` navigation and route.
2. Render connectivity/readiness checklist and setup guide when disconnected.
3. Render NAV, cash, buying power, P&L, positions, orders, deals and complete execution policy.
4. Add limit-order ticket with live Moomoo quote, preview, explicit confirmation phrase and in-memory trade token.
5. Make mobile layout readable and distinguish live-money risk visually.
6. Bump CSS/JS cache versions.

### Task 5: Verify

**Steps:**
1. Run focused tests and full pytest suite.
2. Run Python compile, JavaScript syntax and `git diff --check`.
3. Restart dashboard and verify API status fails closed without OpenD.
4. Verify page route in browser/DOM and mobile-width layout.
5. Commit and push only after all checks pass.
