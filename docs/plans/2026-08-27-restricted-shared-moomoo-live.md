# Restricted Shared-Account Moomoo Live Implementation Plan

> **For Hermes:** Implement task-by-task with TDD and an independent security review before any production activation.

**Goal:** Permit tightly restricted Moomoo live orders in a shared broker account only after an explicit persisted risk acceptance, while never intentionally buying a symbol with external lots or selling more than locally proven strategy-owned shares.

**Architecture:** Add a third isolation state, `shared_restricted`, distinct from `dedicated` and `unverified`. Bind the isolation mode into every signed order preview and revalidate it immediately before broker mutation. Reconciliation may observe unrelated external holdings in shared mode but must freeze on any strategy-symbol quantity mismatch. Production activation remains a separate post-implementation decision.

**Tech Stack:** Python 3.12, FastAPI, SQLite, Moomoo OpenD, pytest, vanilla JavaScript.

---

### Task 1: Add explicit isolation-mode configuration

**Files:**
- Modify: `core/moomoo_client.py`
- Test: `tests/test_moomoo_live_account.py`

1. Add `MOOMOO_SHARED_ACCOUNT_RISK_ACCEPTED`, default false.
2. Expose a single `account_isolation_mode` property: `dedicated`, `shared_restricted`, or `unverified`.
3. Reject configurations where dedicated and shared acceptance are both true.
4. Surface the mode in status and public policy.
5. Add failing tests, implement, rerun focused tests.

### Task 2: Bind isolation mode into preview and placement

**Files:**
- Modify: `core/moomoo_client.py`
- Test: `tests/test_moomoo_live_account.py`

1. Add failing tests proving unverified accounts cannot place orders.
2. Add a shared-restricted placement test proving explicit acceptance is sufficient only when all existing gates pass.
3. Bind `account_isolation_mode` into the signed preview payload.
4. Revalidate the mode during the locked fresh preview immediately before broker mutation.
5. Reject old previews after any isolation-mode change.

### Task 3: Enforce symbol and lot isolation in shared mode

**Files:**
- Modify: `core/moomoo_client.py`
- Modify: `scripts/live_account_sync.py`
- Test: `tests/test_moomoo_live_account.py`
- Test: `tests/test_live_sync.py`

1. Preserve the existing BUY rejection when broker quantity exceeds strategy-owned quantity.
2. Preserve SELL capacity as local owned quantity minus module SELL reservations.
3. Allow unrelated external symbols during reconciliation only in `shared_restricted` or fully read-only mode.
4. Freeze on exact-quantity mismatch for every strategy-owned symbol, including manual App buys/sells and corporate actions.
5. Never import external deals, cash, or positions into the strategy ledger.
6. Add attacks for same-symbol external lots, manual quantity changes, unrelated external symbols, and trading without explicit acceptance.

### Task 4: Harden unfreeze and runtime status

**Files:**
- Modify: `api/live_account.py`
- Modify: `static/js/live_account.js`
- Test: `tests/test_live_control_api.py`

1. Permit unfreeze only for `dedicated` or `shared_restricted` isolation modes.
2. Continue requiring fresh post-freeze reconciliation and zero unknown broker outcomes.
3. Display a prominent `SHARED ACCOUNT — LOGICAL ISOLATION ONLY` warning.
4. Never label shared mode as dedicated or physically isolated.

### Task 5: Document and verify

**Files:**
- Modify: `docs/moomoo-live-account.md`

1. Document residual risk: a manual Moomoo App trade can disturb strategy lots; reconciliation freezes but cannot prevent the broker action.
2. Document that current external symbols are ineligible for strategy BUY.
3. Run focused tests, full pytest, compileall, JS syntax, shell syntax, and `git diff --check`.
4. Run fake-broker attack review and independent reviewer.
5. Commit and push code only after review passes.
6. Keep production `MOOMOO_TRADING_ENABLED=false`, `MOOMOO_AUTO_TRADING_ENABLED=false`, and `MOOMOO_SHARED_ACCOUNT_RISK_ACCEPTED=false` until a separate RTH activation checklist is approved.
