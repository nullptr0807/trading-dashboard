from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from core.live_strategy_control import ControlRejected, LiveStrategyStore

_spec = importlib.util.spec_from_file_location(
    "live_account_sync_under_test",
    Path(__file__).resolve().parents[1] / "scripts" / "live_account_sync.py",
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
reconcile = _module.reconcile


class FakeClient:
    def snapshot(self):
        return {
            "account_id": 1,
            "activity_warnings": [],
            "orders": [
                {"order_id": "module-order", "code": "US.AAPL", "trd_side": "BUY",
                 "order_status": "FILLED_ALL", "dealt_qty": 2, "price": 100,
                 "remark": "dashboard:B16:preview"},
                {"order_id": "manual-order", "code": "US.MSFT", "trd_side": "BUY",
                 "order_status": "FILLED_ALL", "dealt_qty": 50, "price": 100,
                 "remark": "manual"},
            ],
            "deals": [
                {"deal_id": "module-deal", "order_id": "module-order", "code": "US.AAPL",
                 "trd_side": "BUY", "deal_qty": 2, "deal_price": 100},
                {"deal_id": "manual-deal", "order_id": "manual-order", "code": "US.MSFT",
                 "trd_side": "BUY", "deal_qty": 50, "deal_price": 100},
            ],
            "order_fees": [{"order_id": "module-order", "fee_amount": 1.0}],
            "positions": [
                {"code": "US.AAPL", "qty": 2},
            ],
        }

    def quote(self, code):
        assert code == "US.AAPL"
        return {"last_price": 100.0, "source": "Moomoo OpenD"}


def active_store(tmp_path):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    with store.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                    "freeze_reason=NULL,last_sync_at='synced' WHERE id=1")
    return store


def test_reconciliation_imports_only_module_tagged_moomoo_fills(tmp_path):
    store = active_store(tmp_path)
    result = reconcile(FakeClient(), store, ownership_proof=lambda *_: True)
    assert result["applied_fills"] == 1
    assert store.owned_quantity("US.AAPL") == 2
    assert store.owned_quantity("US.MSFT") == 0
    assert store.snapshot().allocated_cash == pytest.approx(9799)
    assert store.snapshot().strategy_equity == pytest.approx(9999)
    second = reconcile(FakeClient(), store, ownership_proof=lambda *_: True)
    assert second["applied_fills"] == 0
    assert store.owned_quantity("US.AAPL") == 2


def test_forged_dashboard_remark_without_local_proof_is_rejected(tmp_path):
    store = active_store(tmp_path)
    with pytest.raises(ControlRejected, match="ownership forgery"):
        reconcile(FakeClient(), store)
    assert store.owned_quantity("US.AAPL") == 0


def test_known_preview_cannot_authorize_modified_broker_order(tmp_path, monkeypatch):
    store = active_store(tmp_path)
    monkeypatch.setattr(_module, "module_preview_record", lambda *_: {
        "order_id": None,
        "payload": {"code": "US.AAPL", "side": "BUY", "qty": 999,
                    "limit_price": 100, "account_id": 1},
    })
    with pytest.raises(ControlRejected, match="ownership forgery"):
        reconcile(FakeClient(), store)
    assert store.owned_quantity("US.AAPL") == 0


def test_reconciliation_fails_if_fee_truth_is_missing(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["order_fees"] = []
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="fee record missing"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.owned_quantity("US.AAPL") == 0


def test_broker_cannot_have_fewer_shares_than_strategy_ledger(tmp_path):
    store = active_store(tmp_path)
    store.apply_fill("existing", "US.AAPL", "BUY", 5, 100)
    client = FakeClient()
    data = client.snapshot()
    data["deals"] = []
    data["positions"][0]["qty"] = 4
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="differs from strategy-owned"):
        reconcile(client, store, ownership_proof=lambda *_: True)


def test_dedicated_account_rejects_any_external_holding(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["positions"].append({"code": "US.MSFT", "qty": 1})
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="external holdings"):
        reconcile(client, store, ownership_proof=lambda *_: True)
