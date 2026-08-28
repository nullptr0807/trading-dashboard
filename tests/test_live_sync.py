from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.live_strategy_control import ControlRejected, LiveStrategyStore
from core.moomoo_client import MoomooSettings

_spec = importlib.util.spec_from_file_location(
    "live_account_sync_under_test",
    Path(__file__).resolve().parents[1] / "scripts" / "live_account_sync.py",
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
reconcile = _module.reconcile


class FakeClient:
    settings = SimpleNamespace(
        account_mode="DEDICATED",
        dedicated_account_confirmed=True,
        shared_account_risk_accepted=False,
        trading_enabled=False,
        auto_trading_enabled=False,
        trade_api_token="",
        password_md5="",
    )

    def snapshot(self):
        return {
            "account_id": 1,
            "activity_warnings": [],
            "orders": [
                {"order_id": "module-order", "code": "US.AAPL", "trd_side": "BUY",
                 "order_status": "FILLED_ALL", "qty": 2, "dealt_qty": 2, "price": 100,
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


def test_shared_first_module_buy_is_not_misclassified_as_external(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode="SHARED_RESTRICTED",
        dedicated_account_confirmed=False,
        shared_account_risk_accepted=True,
        trading_enabled=True,
        auto_trading_enabled=False,
        trade_api_token="",
        password_md5="",
    )
    data = client.snapshot()
    data["positions"][0]["qty"] = 12  # 10 personal + 2 strategy
    client.snapshot = lambda: data
    result = reconcile(client, store, ownership_proof=lambda *_: True)
    assert result["applied_fills"] == 1
    assert store.owned_quantity("US.AAPL") == 2
    assert store.owned_quantity("US.MSFT") == 0


def test_shared_manual_sell_cannot_reduce_broker_below_strategy_owned_qty(tmp_path):
    store = active_store(tmp_path)
    store.apply_fill("strategy-owned", "US.AAPL", "BUY", 2, 100)
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode="SHARED_RESTRICTED",
        dedicated_account_confirmed=False,
        shared_account_risk_accepted=True,
        trading_enabled=True,
        auto_trading_enabled=False,
        trade_api_token="",
        password_md5="",
    )
    data = client.snapshot()
    data["orders"] = []
    data["deals"] = []
    data["order_fees"] = []
    data["positions"] = [{"code": "US.AAPL", "qty": 1}]
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="staged strategy quantity"):
        reconcile(client, store)
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


def test_shared_partial_buy_without_deal_detail_is_rejected(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode="SHARED_RESTRICTED",
        dedicated_account_confirmed=False,
        shared_account_risk_accepted=True,
        trading_enabled=True,
        auto_trading_enabled=True,
        trade_api_token="",
        password_md5="",
    )
    data = client.snapshot()
    data["orders"] = [{
        "order_id": "module-order", "code": "US.AAPL", "trd_side": "BUY",
        "order_status": "CANCELLED_PART", "qty": 10, "dealt_qty": 6,
        "price": 100, "remark": "dashboard:B16:preview",
    }]
    data["deals"] = []
    data["order_fees"] = []
    data["positions"] = [{"code": "US.AAPL", "qty": 6}]
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="differs from deal detail total"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.owned_quantity("US.AAPL") == 0
    assert store.snapshot().allocated_cash == pytest.approx(10_000)
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM applied_fills").fetchone()[0] == 0


def test_filled_status_leading_fill_details_keeps_acked_intent_unresolved(tmp_path):
    store = active_store(tmp_path)
    intent = _acked_intent_for_fake_order(store)
    client = FakeClient()
    data = client.snapshot()
    data["orders"][0].update(order_status="FILLED_ALL", dealt_qty=0)
    data["deals"] = []
    data["order_fees"] = []
    data["positions"] = []
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="filled status leads complete fill details"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    current = store.get_auto_order_intent(intent["intent_id"])
    assert current is not None and current["status"] == "ACKED"
    assert store.positions() == []


def test_delayed_order_visibility_keeps_acked_then_later_recovers(tmp_path):
    store = active_store(tmp_path)
    intent = _acked_intent_for_fake_order(store)
    client = FakeClient()
    lagging = client.snapshot()
    lagging.update(orders=[], deals=[], order_fees=[], positions=[])
    client.snapshot = lambda: lagging

    first = reconcile(client, store, ownership_proof=lambda *_: True)
    current = store.get_auto_order_intent(intent["intent_id"])
    assert first["applied_fills"] == 0
    assert current is not None and current["status"] == "ACKED"

    complete = FakeClient().snapshot()
    client.snapshot = lambda: complete
    second = reconcile(client, store, ownership_proof=lambda *_: True)
    current = store.get_auto_order_intent(intent["intent_id"])
    assert second["applied_fills"] == 1
    assert current is not None and current["status"] == "FILLED"


def test_distinct_deals_apply_once_regardless_of_callback_order(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    first = dict(data["deals"][0], deal_id="deal-first", deal_qty=1, deal_price=99)
    second = dict(data["deals"][0], deal_id="deal-second", deal_qty=1, deal_price=101)
    data["deals"] = [second, first]
    client.snapshot = lambda: data

    result = reconcile(client, store, ownership_proof=lambda *_: True)

    assert result["applied_fills"] == 2
    assert store.owned_quantity("US.AAPL") == 2
    assert len(store.fills(limit=10)) == 2


def test_negative_fee_is_rejected_without_ledger_mutation(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["order_fees"] = [{"order_id": "module-order", "fee_amount": -99}]
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="negative"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.owned_quantity("US.AAPL") == 0
    assert store.snapshot().allocated_cash == pytest.approx(10_000)


def test_deal_symbol_must_match_authorized_order(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["deals"][0]["code"] = "US.MSFT"
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="deal symbol differs"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.positions() == []


@pytest.mark.parametrize("missing_field", ["code", "trd_side"])
def test_deal_must_explicitly_provide_symbol_and_side(tmp_path, missing_field):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["deals"][0].pop(missing_field)
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="explicitly provide symbol and side"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.positions() == []


def test_reconciliation_rejects_legacy_settings_without_explicit_account_mode(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    client.settings = SimpleNamespace(
        dedicated_account_confirmed=True,
        shared_account_risk_accepted=False,
        trading_enabled=False,
        auto_trading_enabled=False,
        trade_api_token="",
        password_md5="",
    )
    with pytest.raises(ControlRejected, match="account_mode"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.positions() == []


def test_shared_mode_requires_explicit_mode_and_risk_acceptance(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    client.settings = MoomooSettings(  # type: ignore[assignment]
        account_mode="SHARED_RESTRICTED",
        shared_account_risk_accepted=True,
        trading_enabled=True,
    )
    result = reconcile(client, store, ownership_proof=lambda *_: True)
    assert result["account_isolation_mode"] == "shared_restricted"
    assert store.owned_quantity("US.AAPL") == 2


def test_quote_failure_rolls_back_entire_reconciliation_batch(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    def unavailable_quote(code):
        raise RuntimeError("quote unavailable")
    client.quote = unavailable_quote
    with pytest.raises(RuntimeError, match="quote unavailable"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.positions() == []
    assert store.snapshot().allocated_cash == pytest.approx(10_000)
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM applied_fills").fetchone()[0] == 0


def _acked_intent_for_fake_order(store):
    intent = store.create_auto_order_intent(
        strategy_id="B16", config_version=1, signal_batch_id="b" * 64,
        signal_source_date="2026-08-26", factor_set_hash="f" * 64,
        symbol="US.AAPL", side="BUY", purpose="TARGET_BUY", target_qty=2,
        order_qty=2, limit_price=100,
    )
    store.mark_auto_intent_dispatching(intent["intent_id"], "preview")
    store.mark_auto_intent_acked(intent["intent_id"])
    return intent


def test_confirmed_fill_auto_recovers_transient_post_broker_freeze(tmp_path, monkeypatch):
    store = active_store(tmp_path)
    intent = _acked_intent_for_fake_order(store)
    store.freeze("auto_post_broker_reconciliation_failed", "auto_executor")
    monkeypatch.setattr(_module, "unresolved_preview_count", lambda: 0)

    result = reconcile(FakeClient(), store, ownership_proof=lambda *_: True)

    assert result["auto_recovered"] is True
    assert store.get_auto_order_intent(intent["intent_id"])["status"] == "FILLED"
    assert store.snapshot().lifecycle == "ACTIVE"
    assert store.snapshot().freeze_reason is None


def test_auto_recovery_waits_if_any_broker_preview_is_unresolved(tmp_path, monkeypatch):
    store = active_store(tmp_path)
    intent = _acked_intent_for_fake_order(store)
    store.freeze("auto_post_broker_reconciliation_failed", "auto_executor")
    monkeypatch.setattr(_module, "unresolved_preview_count", lambda: 1)

    result = reconcile(FakeClient(), store, ownership_proof=lambda *_: True)

    assert result["auto_recovered"] is False
    assert store.get_auto_order_intent(intent["intent_id"])["status"] == "FILLED"
    assert store.snapshot().lifecycle == "FROZEN"


def test_auto_recovery_never_releases_an_unrelated_freeze(tmp_path, monkeypatch):
    store = active_store(tmp_path)
    intent = _acked_intent_for_fake_order(store)
    store.freeze("manual_freeze", "dashboard")
    monkeypatch.setattr(_module, "unresolved_preview_count", lambda: 0)

    result = reconcile(FakeClient(), store, ownership_proof=lambda *_: True)

    assert result["auto_recovered"] is False
    assert store.get_auto_order_intent(intent["intent_id"])["status"] == "FILLED"
    assert store.snapshot().freeze_reason == "manual_freeze"


def test_fill_batch_is_atomic_when_final_broker_quantity_mismatches(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["positions"][0]["qty"] = 3
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="differs from staged strategy quantity"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.owned_quantity("US.AAPL") == 0
    assert store.snapshot().allocated_cash == pytest.approx(10_000)
    assert store.snapshot().lifecycle == "FROZEN"
    assert store.snapshot().freeze_reason == "reconciliation_quantity_mismatch"
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM applied_fills").fetchone()[0] == 0
        row = con.execute(
            "SELECT event_type,details_json FROM strategy_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and row[0] == "reconciliation_quantity_mismatch"
    details = json.loads(row[1])
    assert details == {
        "account_isolation_mode": "dedicated",
        "expected_quantity": 2.0,
        "observed_quantity": 3.0,
        "observation_stage": "broker_position_snapshot_after_deal_staging",
        "observed_at": details["observed_at"],
        "symbol": "US.AAPL",
    }
    assert "account_id" not in row[1]
    assert "order_id" not in row[1]


def test_quantity_mismatch_preserves_historical_fills_and_diagnostic(tmp_path):
    store = active_store(tmp_path)
    store.apply_fill("historical", "US.AAPL", "BUY", 1, 90)
    client = FakeClient()
    data = client.snapshot()
    data["positions"][0]["qty"] = 4
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="differs from staged strategy quantity"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    fills = store.fills(limit=10)
    assert [(row["symbol"], row["quantity"], row["price"]) for row in fills] == [
        ("US.AAPL", 1.0, 90.0),
    ]
    assert store.recent_events(1)[0]["event_type"] == "reconciliation_quantity_mismatch"


def test_real_mismatch_replaces_recoverable_freeze_and_cannot_auto_unfreeze(tmp_path, monkeypatch):
    store = active_store(tmp_path)
    intent = _acked_intent_for_fake_order(store)
    store.freeze("auto_post_broker_reconciliation_failed", "auto_executor")
    client = FakeClient()
    mismatch = client.snapshot()
    mismatch["positions"][0]["qty"] = 3
    client.snapshot = lambda: mismatch

    with pytest.raises(ControlRejected, match="differs from staged strategy quantity"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.snapshot().freeze_reason == "reconciliation_quantity_mismatch"

    monkeypatch.setattr(_module, "unresolved_preview_count", lambda: 0)
    complete = FakeClient().snapshot()
    client.snapshot = lambda: complete
    result = reconcile(client, store, ownership_proof=lambda *_: True)

    assert result["auto_recovered"] is False
    current = store.get_auto_order_intent(intent["intent_id"])
    assert current is not None and current["status"] == "FILLED"
    assert store.snapshot().freeze_reason == "reconciliation_quantity_mismatch"


def test_duplicate_identical_deals_are_idempotently_deduplicated(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["deals"] = [data["deals"][0], dict(data["deals"][0])]
    client.snapshot = lambda: data

    result = reconcile(client, store, ownership_proof=lambda *_: True)

    assert result["applied_fills"] == 1
    assert store.owned_quantity("US.AAPL") == 2


def test_conflicting_duplicate_deal_is_rejected_without_mutation(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    conflict = dict(data["deals"][0], deal_qty=1)
    data["deals"] = [data["deals"][0], conflict]
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="conflicting duplicate"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.positions() == []


def test_restarted_store_recovers_acked_only_after_complete_broker_snapshot(tmp_path):
    store = active_store(tmp_path)
    intent = _acked_intent_for_fake_order(store)
    restarted = LiveStrategyStore(store.path, tmp_path / "archives")

    result = reconcile(FakeClient(), restarted, ownership_proof=lambda *_: True)

    assert result["applied_fills"] == 1
    assert restarted.get_auto_order_intent(intent["intent_id"])["status"] == "FILLED"
    assert restarted.owned_quantity("US.AAPL") == 2


def test_complete_partial_fill_is_applied_once_and_remains_global_blocker(tmp_path):
    store = active_store(tmp_path)
    intent = store.create_auto_order_intent(
        strategy_id="B16", config_version=1, signal_batch_id="b" * 64,
        signal_source_date="2026-08-26", factor_set_hash="f" * 64,
        symbol="US.AAPL", side="BUY", purpose="TARGET_BUY", target_qty=10,
        order_qty=10, limit_price=100,
    )
    store.mark_auto_intent_dispatching(intent["intent_id"], "preview")
    store.mark_auto_intent_acked(intent["intent_id"])
    client = FakeClient()
    data = client.snapshot()
    data["orders"][0].update(order_status="FILLED_PART", qty=10, dealt_qty=6)
    data["deals"][0].update(deal_qty=6)
    data["positions"][0]["qty"] = 6
    client.snapshot = lambda: data

    first = reconcile(client, store, ownership_proof=lambda *_: True)
    second = reconcile(client, store, ownership_proof=lambda *_: True)

    assert first["applied_fills"] == 1
    assert second["applied_fills"] == 0
    assert store.owned_quantity("US.AAPL") == 6
    assert store.get_auto_order_intent(intent["intent_id"])["status"] == "PARTIAL"
    with pytest.raises(ControlRejected, match="unresolved"):
        store.create_auto_order_intent(
            strategy_id="B16", config_version=1, signal_batch_id="c" * 64,
            signal_source_date="2026-08-26", factor_set_hash="f" * 64,
            symbol="US.MSFT", side="BUY", purpose="TARGET_BUY", target_qty=1,
            order_qty=1, limit_price=100,
        )


def test_broker_cannot_have_fewer_shares_than_strategy_ledger(tmp_path):
    store = active_store(tmp_path)
    store.apply_fill("existing", "US.AAPL", "BUY", 5, 100)
    client = FakeClient()
    data = client.snapshot()
    data["orders"] = []
    data["deals"] = []
    data["order_fees"] = []
    data["positions"][0]["qty"] = 4
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="differs from staged strategy quantity"):
        reconcile(client, store, ownership_proof=lambda *_: True)


def test_dedicated_account_rejects_any_external_holding(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["positions"].append({"code": "US.MSFT", "qty": 1})
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="external holdings"):
        reconcile(client, store, ownership_proof=lambda *_: True)


def test_shared_account_read_only_observes_but_never_imports_external_holdings(tmp_path):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode="UNVERIFIED",
        dedicated_account_confirmed=False,
        shared_account_risk_accepted=False,
        trading_enabled=False,
        auto_trading_enabled=False,
        trade_api_token="",
        password_md5="",
    )
    data = client.snapshot()
    data["orders"] = [row for row in data["orders"] if row["order_id"] == "manual-order"]
    data["deals"] = [row for row in data["deals"] if row["order_id"] == "manual-order"]
    data["order_fees"] = []
    data["positions"] = [{"code": "US.MSFT", "qty": 50}]
    client.snapshot = lambda: data

    result = reconcile(client, store)

    assert result["shared_read_only"] is True
    assert result["external_positions"] == 1
    assert result["owned_positions"] == 0
    assert store.positions() == []
    assert store.snapshot().lifecycle == "FROZEN"
    assert store.snapshot().freeze_reason == "not_provisioned"
    assert store.snapshot().last_sync_at is not None


def test_shared_account_external_holdings_fail_if_trading_is_enabled(tmp_path):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode="UNVERIFIED",
        dedicated_account_confirmed=False,
        shared_account_risk_accepted=False,
        trading_enabled=True,
        auto_trading_enabled=False,
        trade_api_token="",
        password_md5="",
    )
    data = client.snapshot()
    data["orders"] = []
    data["deals"] = []
    data["order_fees"] = []
    data["positions"] = [{"code": "US.MSFT", "qty": 1}]
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="external holdings"):
        reconcile(client, store)


def test_shared_restricted_trading_observes_unrelated_external_holdings(tmp_path):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode="SHARED_RESTRICTED",
        dedicated_account_confirmed=False,
        shared_account_risk_accepted=True,
        trading_enabled=True,
        auto_trading_enabled=False,
        trade_api_token="",
        password_md5="",
    )
    data = client.snapshot()
    data["orders"] = []
    data["deals"] = []
    data["order_fees"] = []
    data["positions"] = [{"code": "US.MSFT", "qty": 1}]
    client.snapshot = lambda: data

    result = reconcile(client, store)

    assert result["account_isolation_mode"] == "shared_restricted"
    assert result["external_positions"] == 1
    assert store.positions() == []


def test_external_personal_symbol_remains_transparent_but_not_strategy_owned(tmp_path):
    db = tmp_path / "strategy.db"
    archives = tmp_path / "archives"
    store = LiveStrategyStore(db, archives)
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode="UNVERIFIED",
        dedicated_account_confirmed=False,
        shared_account_risk_accepted=False,
        trading_enabled=False,
        auto_trading_enabled=False,
        trade_api_token="",
        password_md5="",
    )
    data = client.snapshot()
    data["orders"] = []
    data["deals"] = []
    data["order_fees"] = []
    data["positions"] = [{"code": "US.MSFT", "qty": 1}]
    client.snapshot = lambda: data
    reconcile(client, store)
    assert store.owned_quantity("US.MSFT") == 0

    restarted = LiveStrategyStore(db, archives)
    cleared = dict(data)
    cleared["positions"] = []
    client.snapshot = lambda: cleared
    reconcile(client, restarted)
    assert restarted.owned_quantity("US.MSFT") == 0


def test_net_zero_manual_activity_is_allowed_when_broker_still_covers_owned_qty(tmp_path):
    store = active_store(tmp_path)
    store.apply_fill("existing", "US.AAPL", "BUY", 2, 100)
    client = FakeClient()
    data = client.snapshot()
    data["orders"] = [
        {"order_id": "manual-sell", "code": "US.AAPL", "trd_side": "SELL",
         "order_status": "FILLED_ALL", "qty": 1, "dealt_qty": 1, "price": 100,
         "remark": "manual"},
        {"order_id": "manual-buy", "code": "US.AAPL", "trd_side": "BUY",
         "order_status": "FILLED_ALL", "qty": 1, "dealt_qty": 1, "price": 100,
         "remark": "manual"},
    ]
    data["deals"] = [
        {"deal_id": "manual-sell-deal", "order_id": "manual-sell", "code": "US.AAPL",
         "trd_side": "SELL", "deal_qty": 1, "deal_price": 100},
        {"deal_id": "manual-buy-deal", "order_id": "manual-buy", "code": "US.AAPL",
         "trd_side": "BUY", "deal_qty": 1, "deal_price": 100},
    ]
    data["order_fees"] = []
    data["positions"] = [{"code": "US.AAPL", "qty": 2}]
    client.snapshot = lambda: data

    result = reconcile(client, store)
    assert result["ok"] is True
    assert store.owned_quantity("US.AAPL") == 2
    assert store.manual_conflict_symbols() == set()
