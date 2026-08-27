from __future__ import annotations

import pandas as pd
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from pathlib import Path
import hashlib
import uuid
from types import SimpleNamespace

from core.moomoo_audit import (
    append_audit, claim_preview, is_module_preview, nav_history, recent_audit,
    record_nav_snapshot, register_preview,
)
from core.moomoo_client import (
    BrokerOutcomeUnknown, LiveTradeRejected, MoomooClient, MoomooSettings,
)
from core.live_strategy_control import LiveStrategyStore

TEST_ACCOUNT = uuid.uuid4().int % 1_000_000 + 1
TEST_ORDER = uuid.uuid4().hex
TEST_OLD_ORDER = uuid.uuid4().hex
TEST_DEAL = uuid.uuid4().hex


class _Enum:
    US = "US"
    REAL = "REAL"
    FUTUAU = "FUTUAU"
    BUY = "BUY"
    SELL = "SELL"
    NORMAL = "NORMAL"
    CANCEL = "CANCEL"


class FakeTradeContext:
    def __init__(self):
        self.unlock_calls = 0
        self.place_calls = 0
        self.cancel_calls = 0
        self.raise_place = False
        self.accinfo_calls = 0
        self.closed = False

    def close(self): self.closed = True
    def get_acc_list(self):
        return 0, pd.DataFrame([{"acc_id": TEST_ACCOUNT, "trd_env": "REAL", "acc_status": "ACTIVE"}])
    def accinfo_query(self, **kwargs):
        self.accinfo_calls += 1
        return 0, pd.DataFrame([{"total_assets": 12_500.0, "cash": 5_000.0,
                                 "market_val": 7_500.0, "power": 5_000.0}])
    def position_list_query(self, **kwargs):
        return 0, pd.DataFrame([{"code": "US.AAPL", "qty": 10, "can_sell_qty": 10,
                                 "market_val": 1_000.0, "cost_price": 95.0,
                                 "nominal_price": 100.0, "pl_val": 50.0, "pl_ratio": 5.26}])
    def order_list_query(self, **kwargs):
        return 0, pd.DataFrame([{"order_id": TEST_ORDER, "code": "US.AAPL",
                                 "order_status": "SUBMITTED", "remark": "dashboard:B16:test"}])
    def deal_list_query(self, **kwargs):
        return 0, pd.DataFrame([])
    def history_order_list_query(self, **kwargs):
        return 0, pd.DataFrame([{"order_id": TEST_OLD_ORDER, "code": "US.MSFT", "order_status": "FILLED_ALL"}])
    def history_deal_list_query(self, **kwargs):
        return 0, pd.DataFrame([{"deal_id": TEST_DEAL, "code": "US.MSFT", "deal_qty": 2}])
    def order_fee_query(self, **kwargs):
        return 0, pd.DataFrame([{"order_id": order_id, "fee_amount": 1.0}
                                for order_id in kwargs.get("order_id_list", [])])
    def unlock_trade(self, **kwargs):
        self.unlock_calls += 1
        return 0, "ok"
    def place_order(self, **kwargs):
        self.place_calls += 1
        if self.raise_place:
            raise TimeoutError("simulated broker timeout")
        return 0, pd.DataFrame([{"order_id": TEST_ORDER, "order_status": "SUBMITTED", **kwargs}])
    def modify_order(self, **kwargs):
        self.cancel_calls += 1
        return 0, pd.DataFrame([{"order_id": kwargs["order_id"], "order_status": "CANCELLED"}])


class FakeQuoteContext:
    def __init__(self): self.closed = False
    def close(self): self.closed = True
    def get_market_snapshot(self, codes):
        return 0, pd.DataFrame([{"code": codes[0], "last_price": 100.0,
                                 "bid_price": 99.9, "ask_price": 100.1,
                                 "update_time": "2026-08-26 10:00:00",
                                 "sec_status": "NORMAL"}])
    def get_market_state(self, codes):
        return 0, pd.DataFrame([{"code": codes[0], "market_state": "MORNING"}])


class FakeSDK:
    RET_OK = 0
    TrdMarket = TrdEnv = SecurityFirm = TrdSide = OrderType = ModifyOrderOp = _Enum

    def __init__(self):
        self.trade = FakeTradeContext()
        self.quote = FakeQuoteContext()

    def OpenSecTradeContext(self, **kwargs): return self.trade
    def OpenQuoteContext(self, **kwargs): return self.quote


class FakeControl:
    def snapshot(self):
        return SimpleNamespace(lifecycle="ACTIVE", frozen=False, freeze_reason=None,
                               strategy_equity=10_000, owned_market_value=0,
                               reserved_buy_notional=0, allocated_cash=10_000,
                               initial_capital=10_000, exposure_cap=10_000,
                               loss_floor=7_500, realized_pnl=0, unrealized_pnl=0,
                               config_version=1, strategy_id="B16", last_sync_at="synced")
    def pretrade_guard(self, *args, **kwargs):
        return self.snapshot()
    def owned_quantity(self, symbol):
        return 10.0 if symbol == "US.AAPL" else 0.0
    def config(self):
        return {"version": 1, "values": {}}


def client(control_store=None, **overrides):
    base = dict(account_id=TEST_ACCOUNT, trading_enabled=False, trade_api_token="t",
                dedicated_account_confirmed=True,
                password_md5="m", minimum_nav=10_000,
                max_order_notional=2_500, max_limit_deviation_pct=.02)
    base.update(overrides)
    c = MoomooClient(
        MoomooSettings(**base), sdk=FakeSDK(),
        clock=lambda: datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc),
        control_store=control_store or FakeControl(),
    )
    c._port_open = lambda: True
    return c


def test_settings_default_to_fail_closed(monkeypatch):
    for key in ["MOOMOO_TRADING_ENABLED", "MOOMOO_AUTO_TRADING_ENABLED",
                "MOOMOO_TRADE_API_TOKEN", "MOOMOO_TRADE_PASSWORD_MD5"]:
        monkeypatch.delenv(key, raising=False)
    s = MoomooSettings.from_env()
    assert s.security_firm == "FUTUAU"
    assert s.trading_enabled is False
    assert s.auto_trading_enabled is False
    assert s.trade_api_token == ""
    assert s.password_md5 == ""
    assert s.minimum_nav == 10_000


def test_snapshot_and_quote_are_moomoo_only():
    c = client()
    snap = c.snapshot()
    quote = c.quote("AAPL")
    assert snap["source"] == "Moomoo OpenD"
    assert snap["account_id"] == TEST_ACCOUNT
    assert snap["positions"][0]["code"] == "US.AAPL"
    assert snap["orders"][0]["order_id"] == TEST_OLD_ORDER
    assert snap["deals"][0]["deal_id"] == TEST_DEAL
    assert quote["last_price"] == 100.0
    assert quote["source"] == "Moomoo OpenD"


def test_browser_snapshot_uses_five_minute_server_cache():
    c = client()
    first = c.snapshot_cached(300)
    second = c.snapshot_cached(300)
    assert first is second
    assert c._sdk.trade.accinfo_calls == 1


def test_partial_fill_reserves_only_unfilled_buy_quantity():
    class CaptureControl(FakeControl):
        pending = None
        def pretrade_guard(self, *args, **kwargs):
            self.pending = kwargs["pending_buy_notional"]
            return self.snapshot()
    control = CaptureControl()
    c = client(control_store=control)
    c._sdk.trade.order_list_query = lambda **kwargs: (0, pd.DataFrame([{
        "order_id": "PARTIAL", "code": "US.AAPL", "trd_side": "BUY",
        "order_status": "FILLED_PART", "qty": 10, "dealt_qty": 3,
        "price": 100, "remark": "dashboard:B16:test",
    }]))
    c.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)
    assert control.pending == 700


def test_preview_enforces_nav_notional_sellable_and_price_deviation():
    c = client()
    preview = c.preview_order(code="AAPL", side="BUY", qty=10, limit_price=100.5)
    assert preview["notional"] == 1005
    assert preview["place_order_ready"] is False
    with pytest.raises(LiveTradeRejected, match="deviates"):
        c.preview_order(code="AAPL", side="BUY", qty=10, limit_price=110)
    with pytest.raises(LiveTradeRejected, match="server limit"):
        c.preview_order(code="AAPL", side="BUY", qty=30, limit_price=100)
    with pytest.raises(LiveTradeRejected, match="available position"):
        c.preview_order(code="AAPL", side="SELL", qty=11, limit_price=100)


def test_preview_rejects_outside_rth_and_stale_moomoo_quote():
    off_hours = client()
    off_hours._clock = lambda: datetime(2026, 8, 26, 2, 0, tzinfo=timezone.utc)
    with pytest.raises(LiveTradeRejected, match="regular trading hours"):
        off_hours.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)
    stale = client()
    stale._clock = lambda: datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(LiveTradeRejected, match="missing or stale"):
        stale.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)
    closed = client()
    closed._sdk.quote.get_market_state = lambda codes: (0, pd.DataFrame([
        {"code": codes[0], "market_state": "CLOSED"}
    ]))
    with pytest.raises(LiveTradeRejected, match="market state"):
        closed.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)


def test_external_broker_position_is_never_strategy_sellable(tmp_path):
    control = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    with control.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                    "freeze_reason=NULL,last_sync_at='synced' WHERE id=1")
    c = client(control_store=control)
    assert c.snapshot()["positions"][0]["qty"] == 10
    assert control.owned_quantity("US.AAPL") == 0
    with pytest.raises(LiveTradeRejected, match="not acquired"):
        c.preview_order(code="AAPL", side="SELL", qty=1, limit_price=100)
    with pytest.raises(LiveTradeRejected, match="pre-existing/external"):
        c.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)


def test_regular_hours_only_cannot_be_disabled():
    c = client(rth_only=False)
    with pytest.raises(LiveTradeRejected, match="Regular-hours-only"):
        c.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)


def test_real_order_is_impossible_while_server_switch_is_off():
    c = client(trading_enabled=False)
    preview = c.preview_order(code="AAPL", side="BUY", qty=10, limit_price=100)
    with pytest.raises(LiveTradeRejected, match="disabled"):
        c.place_order(preview["preview_token"], "t")
    assert c._sdk.trade.unlock_calls == 0
    assert c._sdk.trade.place_calls == 0


def test_real_order_requires_explicit_nonzero_account_id():
    c = client(trading_enabled=True, account_id=0)
    preview = c.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)
    with pytest.raises(LiveTradeRejected, match="explicitly select"):
        c.place_order(preview["preview_token"], "t")
    assert c._sdk.trade.place_calls == 0


def test_real_order_requires_verified_dedicated_account():
    c = client(trading_enabled=True, dedicated_account_confirmed=False)
    preview = c.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)
    with pytest.raises(LiveTradeRejected, match="dedicated Moomoo"):
        c.place_order(preview["preview_token"], "t")
    assert c._sdk.trade.place_calls == 0


def test_enabled_order_requires_auth_and_exact_unexpired_preview():
    c = client(trading_enabled=True)
    preview = c.preview_order(code="AAPL", side="BUY", qty=10, limit_price=100)
    with pytest.raises(LiveTradeRejected, match="authorization"):
        c.place_order(preview["preview_token"], "wrong")
    with pytest.raises(LiveTradeRejected, match="Invalid order preview"):
        c.place_order(preview["preview_token"][:-3] + "abc", "t")
    result = c.place_order(preview["preview_token"], "t")
    assert result["accepted"] is True
    assert result["order"]["order_id"] == TEST_ORDER
    assert c._sdk.trade.unlock_calls == 1
    assert c._sdk.trade.place_calls == 1
    with pytest.raises(LiveTradeRejected, match="already used"):
        c.place_order(preview["preview_token"], "t")
    assert c._sdk.trade.place_calls == 1


def test_config_reload_freezes_and_invalidates_old_preview(tmp_path):
    control = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    with control.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                    "freeze_reason=NULL,last_sync_at='synced' WHERE id=1")
    c = client(control_store=control, trading_enabled=True)
    preview = c.preview_order(code="MSFT", side="BUY", qty=1, limit_price=100)
    version = control.config()["version"]
    control.update_config({"stop_cooldown_hours": 48}, version, "test", "risk review")
    with pytest.raises(LiveTradeRejected, match="FROZEN"):
        c.place_order(preview["preview_token"], "t")
    assert c._sdk.trade.place_calls == 0


def test_broker_timeout_is_unknown_and_preview_cannot_be_retried():
    c = client(trading_enabled=True)
    preview = c.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)
    c._sdk.trade.raise_place = True
    with pytest.raises(BrokerOutcomeUnknown, match="reconcile"):
        c.place_order(preview["preview_token"], "t")
    with pytest.raises(LiveTradeRejected, match="already used"):
        c.place_order(preview["preview_token"], "t")


def test_unknown_broker_outcome_can_still_be_cancelled_on_freeze(monkeypatch):
    import core.moomoo_client as client_module
    c = client(trading_enabled=True)
    monkeypatch.setattr(client_module, "is_module_order", lambda *args, **kwargs: False)
    monkeypatch.setattr(client_module, "is_module_preview", lambda *args, **kwargs: True)
    result = c.cancel_all_module_orders("t")
    assert result["requested"] == 1
    assert result["cancelled"] == 1
    assert not result["errors"]


def test_cancel_remains_available_when_new_order_master_switch_is_off():
    c = client(trading_enabled=False)
    result = c.cancel_order(TEST_ORDER, "t")
    assert result["accepted"] is True
    assert c._sdk.trade.cancel_calls == 1


def test_audit_db_separates_nav_history_and_strips_secrets(tmp_path):
    path = tmp_path / "audit.db"
    record_nav_snapshot(TEST_ACCOUNT, {"total_assets": 12_500, "cash": 3_000,
                              "market_val": 9_500}, "USD", path=path)
    append_audit("preview", True, {"account_id": TEST_ACCOUNT, "code": "US.AAPL",
                                   "preview_token": "must-not-persist"}, path=path)
    history = nav_history(TEST_ACCOUNT, path=path)
    events = recent_audit(path=path)
    assert history[0]["total_assets"] == 12_500
    assert events[0]["action"] == "preview"
    assert "must-not-persist" not in events[0]["detail"]


def test_preview_claim_is_atomic_and_one_time(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "audit.db"
    preview_id = uuid.uuid4().hex
    payload = {"preview_id": preview_id, "account_id": TEST_ACCOUNT}
    register_preview(payload, 90, path=path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: claim_preview(preview_id, path=path), range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_claimed_or_unknown_preview_can_authorize_emergency_cancel(tmp_path):
    path = tmp_path / "audit.db"
    preview_id = uuid.uuid4().hex
    payload = {"preview_id": preview_id, "account_id": TEST_ACCOUNT}
    register_preview(payload, 90, path=path)
    assert claim_preview(preview_id, path=path)
    assert is_module_preview(preview_id, TEST_ACCOUNT, path=path)
    assert not is_module_preview(preview_id, TEST_ACCOUNT + 1, path=path)


def test_missing_cash_and_sellable_fields_fail_closed():
    buy = client()
    buy._sdk.trade.accinfo_query = lambda **kwargs: (0, pd.DataFrame([{"total_assets": 12_500}]))
    with pytest.raises(LiveTradeRejected, match="field cash is missing"):
        buy.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)
    sell = client()
    sell._sdk.trade.position_list_query = lambda **kwargs: (0, pd.DataFrame([
        {"code": "US.AAPL", "qty": 10, "market_val": 1_000}
    ]))
    with pytest.raises(LiveTradeRejected, match="field can_sell_qty is missing"):
        sell.preview_order(code="AAPL", side="SELL", qty=1, limit_price=100)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_non_finite_or_negative_risk_limits_fail_closed(bad):
    c = client(max_order_notional=bad)
    with pytest.raises(LiveTradeRejected, match="Invalid live-trade configuration"):
        c.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)


def test_live_account_financial_data_requires_separate_read_token(monkeypatch):
    import api.live_account as live_api
    from server import app
    c = client(read_api_token="r")
    monkeypatch.setattr(live_api, "_client", c)
    http = TestClient(app)
    public = http.get("/api/live-account/status").json()["status"]
    assert public["read_access_granted"] is False
    assert public["account_id"] is None
    assert http.get("/api/live-account/snapshot").status_code == 401
    private = http.get("/api/live-account/status", headers={"X-Moomoo-Read-Token": "r"}).json()["status"]
    assert private["read_access_granted"] is True
    assert private["account_id"] == TEST_ACCOUNT


def test_dashboard_executes_no_third_party_javascript_and_vendor_hashes_are_pinned():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static/index.html").read_text()
    assert '<script src="http' not in html
    expected = {
        "static/vendor/katex/katex.min.js": "9f45307c5794ed247a0d095f3a62e52ef2215a67b2327203a7fd919959ae79d1",
        "static/vendor/katex/auto-render.min.js": "7b57d427ac6270677daf8d8380ded2cc73336f9149a167b8e1fe0d6ef66604ae",
        "static/vendor/lightweight-charts/lightweight-charts.standalone.production.js": "78d2bcbd79556d4f67ae3e3f7776f74e3b46a499466615b1f99397c53cb4056f",
        "static/vendor/marked/marked.min.js": "15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894",
    }
    for rel, digest in expected.items():
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == digest
