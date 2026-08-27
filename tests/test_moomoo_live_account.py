from __future__ import annotations

import pandas as pd
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from pathlib import Path
import hashlib

from core.moomoo_audit import (
    append_audit, claim_preview, nav_history, recent_audit,
    record_nav_snapshot, register_preview,
)
from core.moomoo_client import (
    BrokerOutcomeUnknown, LiveTradeRejected, MoomooClient, MoomooSettings,
)


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
        self.closed = False

    def close(self): self.closed = True
    def get_acc_list(self):
        return 0, pd.DataFrame([{"acc_id": 123, "trd_env": "REAL", "acc_status": "ACTIVE"}])
    def accinfo_query(self, **kwargs):
        return 0, pd.DataFrame([{"total_assets": 12_500.0, "cash": 5_000.0,
                                 "market_val": 7_500.0, "power": 5_000.0}])
    def position_list_query(self, **kwargs):
        return 0, pd.DataFrame([{"code": "US.AAPL", "qty": 10, "can_sell_qty": 10,
                                 "market_val": 1_000.0, "cost_price": 95.0,
                                 "nominal_price": 100.0, "pl_val": 50.0, "pl_ratio": 5.26}])
    def order_list_query(self, **kwargs):
        return 0, pd.DataFrame([{"order_id": "OID-1", "code": "US.AAPL",
                                 "order_status": "SUBMITTED", "remark": "dashboard:B16:test"}])
    def deal_list_query(self, **kwargs):
        return 0, pd.DataFrame([])
    def history_order_list_query(self, **kwargs):
        return 0, pd.DataFrame([{"order_id": "OLD-1", "code": "US.MSFT", "order_status": "FILLED_ALL"}])
    def history_deal_list_query(self, **kwargs):
        return 0, pd.DataFrame([{"deal_id": "DEAL-1", "code": "US.MSFT", "deal_qty": 2}])
    def unlock_trade(self, **kwargs):
        self.unlock_calls += 1
        return 0, "ok"
    def place_order(self, **kwargs):
        self.place_calls += 1
        if self.raise_place:
            raise TimeoutError("simulated broker timeout")
        return 0, pd.DataFrame([{"order_id": "OID-1", "order_status": "SUBMITTED", **kwargs}])
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


class FakeSDK:
    RET_OK = 0
    TrdMarket = TrdEnv = SecurityFirm = TrdSide = OrderType = ModifyOrderOp = _Enum

    def __init__(self):
        self.trade = FakeTradeContext()
        self.quote = FakeQuoteContext()

    def OpenSecTradeContext(self, **kwargs): return self.trade
    def OpenQuoteContext(self, **kwargs): return self.quote


def client(**overrides):
    base = dict(account_id=123, trading_enabled=False, trade_api_token="secret-token",
                password_md5="md5-secret", minimum_nav=10_000,
                max_order_notional=2_500, max_limit_deviation_pct=.02)
    base.update(overrides)
    c = MoomooClient(
        MoomooSettings(**base), sdk=FakeSDK(),
        clock=lambda: datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc),
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
    assert snap["account_id"] == 123
    assert snap["positions"][0]["code"] == "US.AAPL"
    assert snap["orders"][0]["order_id"] == "OLD-1"
    assert snap["deals"][0]["deal_id"] == "DEAL-1"
    assert quote["last_price"] == 100.0
    assert quote["source"] == "Moomoo OpenD"


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


def test_real_order_is_impossible_while_server_switch_is_off():
    c = client(trading_enabled=False)
    preview = c.preview_order(code="AAPL", side="BUY", qty=10, limit_price=100)
    with pytest.raises(LiveTradeRejected, match="disabled"):
        c.place_order(preview["preview_token"], "secret-token")
    assert c._sdk.trade.unlock_calls == 0
    assert c._sdk.trade.place_calls == 0


def test_real_order_requires_explicit_nonzero_account_id():
    c = client(trading_enabled=True, account_id=0)
    preview = c.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)
    with pytest.raises(LiveTradeRejected, match="explicitly select"):
        c.place_order(preview["preview_token"], "secret-token")
    assert c._sdk.trade.place_calls == 0


def test_enabled_order_requires_auth_and_exact_unexpired_preview():
    c = client(trading_enabled=True)
    preview = c.preview_order(code="AAPL", side="BUY", qty=10, limit_price=100)
    with pytest.raises(LiveTradeRejected, match="authorization"):
        c.place_order(preview["preview_token"], "wrong")
    with pytest.raises(LiveTradeRejected, match="Invalid order preview"):
        c.place_order(preview["preview_token"][:-3] + "abc", "secret-token")
    result = c.place_order(preview["preview_token"], "secret-token")
    assert result["accepted"] is True
    assert result["order"]["order_id"] == "OID-1"
    assert c._sdk.trade.unlock_calls == 1
    assert c._sdk.trade.place_calls == 1
    with pytest.raises(LiveTradeRejected, match="already used"):
        c.place_order(preview["preview_token"], "secret-token")
    assert c._sdk.trade.place_calls == 1


def test_broker_timeout_is_unknown_and_preview_cannot_be_retried():
    c = client(trading_enabled=True)
    preview = c.preview_order(code="AAPL", side="BUY", qty=1, limit_price=100)
    c._sdk.trade.raise_place = True
    with pytest.raises(BrokerOutcomeUnknown, match="reconcile"):
        c.place_order(preview["preview_token"], "secret-token")
    with pytest.raises(LiveTradeRejected, match="already used"):
        c.place_order(preview["preview_token"], "secret-token")


def test_cancel_is_guarded_and_uses_real_environment():
    c = client(trading_enabled=True)
    result = c.cancel_order("OID-1", "secret-token")
    assert result["accepted"] is True
    assert c._sdk.trade.cancel_calls == 1


def test_audit_db_separates_nav_history_and_strips_secrets(tmp_path):
    path = tmp_path / "audit.db"
    record_nav_snapshot(123, {"total_assets": 12_500, "cash": 3_000,
                              "market_val": 9_500}, "USD", path=path)
    append_audit("preview", True, {"account_id": 123, "code": "US.AAPL",
                                   "preview_token": "must-not-persist"}, path=path)
    history = nav_history(123, path=path)
    events = recent_audit(path=path)
    assert history[0]["total_assets"] == 12_500
    assert events[0]["action"] == "preview"
    assert "must-not-persist" not in events[0]["detail"]


def test_preview_claim_is_atomic_and_one_time(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "audit.db"
    payload = {"preview_id": "P1", "account_id": 123}
    register_preview(payload, 90, path=path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: claim_preview("P1", path=path), range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7


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
    c = client(read_api_token="read-secret")
    monkeypatch.setattr(live_api, "_client", c)
    http = TestClient(app)
    public = http.get("/api/live-account/status").json()["status"]
    assert public["read_access_granted"] is False
    assert public["account_id"] is None
    assert http.get("/api/live-account/snapshot").status_code == 401
    private = http.get("/api/live-account/status", headers={"X-Moomoo-Read-Token": "read-secret"}).json()["status"]
    assert private["read_access_granted"] is True
    assert private["account_id"] == 123


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
