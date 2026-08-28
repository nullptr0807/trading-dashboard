from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import logging
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.live_logging import JsonFormatter
from core.live_strategy_control import ControlRejected, LiveStrategyStore
from core.moomoo_audit import (
    claim_preview, finalize_preview, is_module_order, is_module_preview,
    module_preview_record, register_preview,
)
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


def bind_fake_order_to_real_preview(tmp_path, monkeypatch):
    """Use the real audit DB API while exercising reconcile's default proof."""
    audit_db = tmp_path / "audit.db"
    payload = {
        "preview_id": "preview", "account_id": 1, "code": "US.AAPL",
        "side": "BUY", "qty": 2, "limit_price": 100,
    }
    register_preview(payload, 60, path=audit_db)
    assert claim_preview("preview", path=audit_db)
    finalize_preview("preview", "accepted", "module-order", path=audit_db)
    monkeypatch.setattr(
        _module, "module_preview_record",
        lambda preview_id, account_id: module_preview_record(
            preview_id, account_id, path=audit_db,
        ),
    )
    monkeypatch.setattr(
        _module, "is_module_preview",
        lambda preview_id, account_id: is_module_preview(preview_id, account_id, path=audit_db),
    )
    monkeypatch.setattr(
        _module, "is_module_order",
        lambda order_id, account_id: is_module_order(order_id, account_id, path=audit_db),
    )


def bind_claimed_preview(tmp_path, monkeypatch, *, reconcile_status=False):
    """Bind the fake remark to durable local pre-finalization ownership."""
    audit_db = tmp_path / "claimed-audit.db"
    payload = {
        "preview_id": "preview", "account_id": 1, "code": "US.AAPL",
        "side": "BUY", "qty": 2, "limit_price": 100,
    }
    register_preview(payload, 60, path=audit_db)
    assert claim_preview("preview", path=audit_db)
    if reconcile_status:
        finalize_preview("preview", "unknown", path=audit_db)
    monkeypatch.setattr(
        _module, "module_preview_record",
        lambda preview_id, account_id: module_preview_record(
            preview_id, account_id, path=audit_db,
        ),
    )
    monkeypatch.setattr(
        _module, "is_module_preview",
        lambda preview_id, account_id: is_module_preview(preview_id, account_id, path=audit_db),
    )
    monkeypatch.setattr(
        _module, "is_module_order",
        lambda order_id, account_id: is_module_order(order_id, account_id, path=audit_db),
    )


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
        "preview_id": "preview", "account_id": "1", "status": "claimed",
        "order_id": None,
        "payload": {"code": "US.AAPL", "side": "BUY", "qty": 999,
                    "limit_price": 100, "account_id": 1},
    })
    with pytest.raises(ControlRejected, match="differs"):
        reconcile(FakeClient(), store)
    assert store.owned_quantity("US.AAPL") == 0
    assert store.snapshot().freeze_reason == "reconciliation_snapshot_order_conflict"


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


@pytest.mark.parametrize("location,key", [
    ("order_fees", "fee_amount"),
    ("deals", "deal_fee"),
    ("deals", "commission"),
])
def test_negative_fee_is_rejected_without_ledger_mutation(tmp_path, location, key):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data[location][0][key] = -99
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="negative"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.owned_quantity("US.AAPL") == 0
    assert store.snapshot().allocated_cash == pytest.approx(10_000)


@pytest.mark.parametrize("location,key", [
    ("order_fees", "fee_amount"),
    ("deals", "deal_fee"),
    ("deals", "commission"),
])
@pytest.mark.parametrize("bad_fee", [float("nan"), float("inf"), "not-a-number"])
def test_non_finite_or_invalid_fee_is_rejected_without_ledger_mutation(
    tmp_path, location, key, bad_fee,
):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data[location][0][key] = bad_fee
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="invalid numeric"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.positions() == []
    assert store.execution_summary()["total_trades"] == 0


def test_deal_symbol_must_match_authorized_order(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["deals"][0]["code"] = "US.MSFT"
    client.snapshot = lambda: data
    with pytest.raises(ControlRejected, match="deal symbol differs"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.positions() == []
    assert store.snapshot().freeze_reason == "reconciliation_snapshot_order_conflict"


def test_deal_side_conflict_latches_nonrecoverable_freeze(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["deals"][0]["trd_side"] = "SELL"
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="deal side differs"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.positions() == []
    assert store.snapshot().freeze_reason == "reconciliation_snapshot_order_conflict"
    assert store.recent_events(1)[0]["details"]["conflicting_fields"] == ["side"]


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


def test_quantity_mismatch_keeps_write_lock_until_freeze_and_diagnostic_commit(
    tmp_path, monkeypatch,
):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["positions"][0]["qty"] = 3
    client.snapshot = lambda: data
    original_event_tx = store._event_tx
    writer_started = threading.Event()
    writer_finished = threading.Event()
    observed_lifecycle = []
    statements = []
    thread = None
    original_connect = store.connect

    @contextmanager
    def traced_connect():
        with original_connect() as con:
            con.set_trace_callback(statements.append)
            yield con

    monkeypatch.setattr(store, "connect", traced_connect)

    def competing_writer():
        writer_started.set()
        with store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            observed_lifecycle.append(con.execute(
                "SELECT lifecycle FROM strategy_state WHERE id=1"
            ).fetchone()[0])
        writer_finished.set()

    def event_with_concurrency_probe(con, event_type, *args, **kwargs):
        nonlocal thread
        if event_type == "reconciliation_quantity_mismatch":
            thread = threading.Thread(target=competing_writer)
            thread.start()
            assert writer_started.wait(1)
            time.sleep(0.05)
            assert not writer_finished.is_set()
        return original_event_tx(con, event_type, *args, **kwargs)

    monkeypatch.setattr(store, "_event_tx", event_with_concurrency_probe)
    with pytest.raises(ControlRejected, match="differs from staged strategy quantity"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert thread is not None
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert observed_lifecycle == ["FROZEN"]
    assert not any(statement.strip().upper() == "ROLLBACK" for statement in statements)
    assert store.snapshot().freeze_reason == "reconciliation_quantity_mismatch"
    assert store.recent_events(1)[0]["event_type"] == "reconciliation_quantity_mismatch"


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


def test_duplicate_deal_numeric_types_and_aliases_are_semantically_idempotent(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    original = data["deals"][0]
    alias_replay = {
        "deal_id": original["deal_id"], "order_id": original["order_id"],
        "code": "us.aapl", "trd_side": "buy", "qty": 2.0, "price": 100.0,
    }
    data["deals"] = [original, alias_replay]
    client.snapshot = lambda: data

    result = reconcile(client, store, ownership_proof=lambda *_: True)

    assert result["applied_fills"] == 1
    assert store.owned_quantity("US.AAPL") == 2


def test_conflicting_numeric_aliases_are_rejected_without_mutation(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["deals"][0]["qty"] = 3
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="conflicting numeric aliases"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.positions() == []
    assert store.snapshot().freeze_reason == "reconciliation_snapshot_numeric_conflict"
    assert store.recent_events(1)[0]["details"]["conflicting_fields"] == ["quantity"]


@pytest.mark.parametrize("key,bad", [("qty", "bad"), ("deal_price", float("nan"))])
def test_invalid_deal_numeric_alias_latches_nonrecoverable_conflict(tmp_path, key, bad):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["deals"][0][key] = bad
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="invalid numeric"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.positions() == []
    assert store.snapshot().freeze_reason == "reconciliation_snapshot_numeric_conflict"
    assert store.recent_events(1)[0]["event_type"] == "reconciliation_snapshot_numeric_conflict"


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
    state = store.snapshot()
    assert state.lifecycle == "FROZEN"
    assert state.freeze_reason == "reconciliation_snapshot_deal_conflict"
    event = store.recent_events(1)[0]
    assert event["event_type"] == "reconciliation_snapshot_deal_conflict"
    assert event["details"] == {
        "conflicting_fields": ["quantity"],
        "symbol": "US.AAPL",
    }
    serialized = json.dumps(event, sort_keys=True)
    for secret_name in ("order_id", "deal_id", "account_id", "module-order", "module-deal"):
        assert secret_name not in serialized


def test_snapshot_deal_conflict_keeps_write_lock_through_diagnostic_commit(tmp_path, monkeypatch):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["deals"] = [data["deals"][0], dict(data["deals"][0], deal_qty=1)]
    client.snapshot = lambda: data
    original_event_tx = store._event_tx
    writer_started = threading.Event()
    writer_finished = threading.Event()
    observed = []
    thread = None

    def competing_writer():
        writer_started.set()
        with store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            observed.append(con.execute(
                "SELECT lifecycle,freeze_reason FROM strategy_state WHERE id=1"
            ).fetchone())
        writer_finished.set()

    def event_with_probe(con, event_type, *args, **kwargs):
        nonlocal thread
        if event_type == "reconciliation_snapshot_deal_conflict":
            thread = threading.Thread(target=competing_writer)
            thread.start()
            assert writer_started.wait(1)
            time.sleep(0.05)
            assert not writer_finished.is_set()
        return original_event_tx(con, event_type, *args, **kwargs)

    monkeypatch.setattr(store, "_event_tx", event_with_probe)
    with pytest.raises(ControlRejected, match="conflicting duplicate"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert thread is not None
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert [tuple(row) for row in observed] == [
        ("FROZEN", "reconciliation_snapshot_deal_conflict"),
    ]


def test_snapshot_deal_conflict_replaces_transient_freeze_and_clean_sync_cannot_recover(
    tmp_path, monkeypatch,
):
    store = active_store(tmp_path)
    store.freeze("auto_post_broker_reconciliation_failed", "auto_executor")
    client = FakeClient()
    conflict = client.snapshot()
    conflict["deals"] = [conflict["deals"][0], dict(conflict["deals"][0], deal_price=101)]
    client.snapshot = lambda: conflict

    with pytest.raises(ControlRejected, match="conflicting duplicate"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.snapshot().freeze_reason == "reconciliation_snapshot_deal_conflict"

    monkeypatch.setattr(_module, "unresolved_preview_count", lambda: 0)
    client.snapshot = FakeClient().snapshot
    result = reconcile(client, store, ownership_proof=lambda *_: True)
    assert result["auto_recovered"] is False
    assert store.snapshot().freeze_reason == "reconciliation_snapshot_deal_conflict"


@pytest.mark.parametrize("changed", [
    {"deal_price": 101},
    {"deal_qty": 1},
])
def test_cross_sync_conflicting_deal_reference_freezes_without_rewriting_history(
    tmp_path, changed,
):
    store = active_store(tmp_path)
    client = FakeClient()
    reconcile(client, store, ownership_proof=lambda *_: True)
    original_fills = store.fills(limit=10)
    replay = client.snapshot()
    replay["deals"][0].update(changed)
    replay["orders"][0]["dealt_qty"] = replay["deals"][0].get("deal_qty", 2)
    replay["orders"][0]["qty"] = replay["orders"][0]["dealt_qty"]
    replay["positions"][0]["qty"] = 2
    client.snapshot = lambda: replay

    with pytest.raises(ControlRejected, match="conflicting replay"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.fills(limit=10) == original_fills
    assert store.owned_quantity("US.AAPL") == 2
    assert store.snapshot().freeze_reason == "reconciliation_fill_conflict"
    event = store.recent_events(1)[0]
    assert event["event_type"] == "reconciliation_fill_conflict"
    serialized = json.dumps(event, sort_keys=True)
    for secret_name in (
        "order_id", "deal_id", "account_id", "credential", "module-order", "module-deal",
    ):
        assert secret_name not in serialized


def test_cross_sync_identical_deal_alias_replay_is_idempotent(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    first = reconcile(client, store, ownership_proof=lambda *_: True)
    replay = client.snapshot()
    replay["deals"][0].pop("deal_qty")
    replay["deals"][0].pop("deal_price")
    replay["deals"][0].update(qty=2.0, price=100.0, code="us.aapl", trd_side="buy")
    client.snapshot = lambda: replay

    second = reconcile(client, store, ownership_proof=lambda *_: True)

    assert first["applied_fills"] == 1
    assert second["applied_fills"] == 0
    assert store.owned_quantity("US.AAPL") == 2


def test_cross_sync_cumulative_order_fee_change_is_audited_not_a_fill_conflict(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    reconcile(client, store, ownership_proof=lambda *_: True)
    original_fills = store.fills(limit=10)
    replay = client.snapshot()
    replay["order_fees"][0]["fee_amount"] = 2.0
    client.snapshot = lambda: replay

    reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.fills(limit=10) == original_fills
    assert store.snapshot().freeze_reason is None
    assert store.execution_summary()["total_fees"] == pytest.approx(2.0)
    assert store.recent_events(2)[0]["event_type"] == "account_sync"
    assert any(
        event["event_type"] == "order_fee_adjusted"
        and event["details"]["delta"] == pytest.approx(1.0)
        for event in store.recent_events(4)
    )

    correction = client.snapshot()
    correction["order_fees"][0]["fee_amount"] = 1.5
    client.snapshot = lambda: correction
    reconcile(client, store, ownership_proof=lambda *_: True)
    reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.execution_summary()["total_fees"] == pytest.approx(1.5)
    assert store.snapshot().allocated_cash == pytest.approx(9798.5)
    assert store.snapshot().freeze_reason is None
    assert any(
        event["event_type"] == "order_fee_adjusted"
        and event["details"]["delta"] == pytest.approx(-0.5)
        for event in store.recent_events(4)
    )


def test_cross_sync_stable_deal_fee_conflict_is_latched(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    first = client.snapshot()
    first["deals"][0]["deal_fee"] = 1.0
    client.snapshot = lambda: first
    reconcile(client, store, ownership_proof=lambda *_: True)
    original_fills = store.fills(limit=10)

    replay = client.snapshot()
    replay["deals"][0]["deal_fee"] = 2.0
    replay["order_fees"][0]["fee_amount"] = 2.0
    client.snapshot = lambda: replay
    with pytest.raises(ControlRejected, match="conflicting replay"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.fills(limit=10) == original_fills
    assert store.snapshot().freeze_reason == "reconciliation_fill_conflict"
    assert store.recent_events(1)[0]["details"]["conflicting_fields"] == ["fee"]


def test_delayed_stable_deal_fee_binds_once_without_double_charging(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.snapshot().allocated_cash == pytest.approx(9799.0)

    delayed = client.snapshot()
    delayed["deals"][0]["deal_fee"] = 0.6
    client.snapshot = lambda: delayed
    reconcile(client, store, ownership_proof=lambda *_: True)

    restarted = LiveStrategyStore(store.path, tmp_path / "archives")
    reconcile(client, restarted, ownership_proof=lambda *_: True)
    assert restarted.snapshot().allocated_cash == pytest.approx(9799.0)
    assert restarted.execution_summary()["total_fees"] == pytest.approx(1.0)
    with restarted.connect() as con:
        fill = con.execute(
            "SELECT fee,fee_is_stable,order_hash FROM applied_fills"
        ).fetchone()
        adjustments = con.execute(
            "SELECT previous_total,new_total,fill_fee_credit,delta "
            "FROM order_fee_adjustments ORDER BY id"
        ).fetchall()
    assert tuple(fill[0:2]) == pytest.approx((0.6, 1))
    assert fill[2] == hashlib.sha256(b"module-order").hexdigest()
    assert [tuple(row) for row in adjustments] == [
        (0.0, 1.0, 0.0, 1.0),
        (1.0, 1.0, 0.6, -0.6),
    ]


def test_delayed_stable_fee_binding_rolls_back_and_replays_atomically(tmp_path, monkeypatch):
    store = active_store(tmp_path)
    client = FakeClient()
    reconcile(client, store, ownership_proof=lambda *_: True)
    delayed = client.snapshot()
    delayed["deals"][0]["deal_fee"] = 0.6
    client.snapshot = lambda: delayed
    original_event_tx = store._event_tx

    def crash(con, event_type, *args, **kwargs):
        if event_type == "order_fee_adjusted":
            raise RuntimeError("binding rollback probe")
        return original_event_tx(con, event_type, *args, **kwargs)

    monkeypatch.setattr(store, "_event_tx", crash)
    with pytest.raises(RuntimeError, match="rollback probe"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    with store.connect() as con:
        assert tuple(con.execute(
            "SELECT fee,fee_is_stable FROM applied_fills"
        ).fetchone()) == (0.0, 0)
        assert con.execute("SELECT COUNT(*) FROM order_fee_adjustments").fetchone()[0] == 1

    monkeypatch.setattr(store, "_event_tx", original_event_tx)
    reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.execution_summary()["total_fees"] == pytest.approx(1.0)


def test_deal_cannot_be_reassociated_to_a_different_order(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    reconcile(client, store, ownership_proof=lambda *_: True)
    replay = client.snapshot()
    replay["orders"][0]["order_id"] = "different-order"
    replay["order_fees"][0]["order_id"] = "different-order"
    replay["deals"][0]["order_id"] = "different-order"
    client.snapshot = lambda: replay

    with pytest.raises(ControlRejected, match="different order"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.snapshot().freeze_reason == "reconciliation_fill_conflict"
    assert store.execution_summary()["total_fees"] == pytest.approx(1.0)
    assert store.recent_events(1)[0]["details"]["conflicting_fields"] == [
        "order_ownership"
    ]


def test_same_snapshot_deal_reference_cannot_belong_to_two_orders(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["orders"].append(dict(data["orders"][0], order_id="second-order"))
    data["deals"].append(dict(data["deals"][0], order_id="second-order"))
    data["order_fees"].append({"order_id": "second-order", "fee_amount": 1.0})
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="multiple orders"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.positions() == []
    assert store.snapshot().freeze_reason == "reconciliation_snapshot_order_conflict"
    assert store.recent_events(1)[0]["details"]["conflicting_fields"] == [
        "order_ownership"
    ]


def test_duplicate_order_reference_with_conflicting_economics_latches(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["orders"].append(dict(data["orders"][0], code="US.MSFT"))
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="duplicate order"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.snapshot().freeze_reason == "reconciliation_snapshot_order_conflict"


def test_duplicate_order_fee_reference_with_conflicting_total_latches(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["order_fees"].append({"order_id": "module-order", "fee_amount": 2.0})
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="duplicate fee"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.snapshot().freeze_reason == "reconciliation_snapshot_numeric_conflict"
    assert store.recent_events(1)[0]["details"]["conflicting_fields"] == ["fee"]


def test_legacy_null_order_hash_binds_once_then_is_immutable(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    reconcile(client, store, ownership_proof=lambda *_: True)
    with store.connect() as con:
        con.execute("UPDATE applied_fills SET order_hash=NULL")

    reconcile(client, store, ownership_proof=lambda *_: True)
    with store.connect() as con:
        bound = con.execute("SELECT order_hash FROM applied_fills").fetchone()[0]
    assert bound == hashlib.sha256(b"module-order").hexdigest()

    replay = client.snapshot()
    replay["orders"][0]["order_id"] = "different-order"
    replay["order_fees"][0]["order_id"] = "different-order"
    replay["deals"][0]["order_id"] = "different-order"
    client.snapshot = lambda: replay
    with pytest.raises(ControlRejected, match="different order"):
        reconcile(client, store, ownership_proof=lambda *_: True)


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


def _cumulative_fee_snapshot(*, quantities, prices, cumulative_fee, status="FILLED_PART"):
    client = FakeClient()
    data = client.snapshot()
    deals = []
    for index, (quantity, price) in enumerate(zip(quantities, prices), start=1):
        deals.append(dict(
            data["deals"][0], deal_id=f"module-deal-{index}",
            deal_qty=quantity, deal_price=price,
        ))
    dealt = sum(quantities)
    data["orders"][0].update(order_status=status, qty=10, dealt_qty=dealt)
    data["deals"] = deals
    data["order_fees"] = [{"order_id": "module-order", "fee_amount": cumulative_fee}]
    data["positions"] = [{"code": "US.AAPL", "qty": dealt}]
    return data


@pytest.mark.parametrize("final_fee", [1.2, 1.6, 0.8])
def test_six_of_ten_partial_to_final_fee_correction_keeps_all_totals_consistent(
    tmp_path, final_fee,
):
    store = active_store(tmp_path)
    client = FakeClient()
    client.snapshot = lambda: _cumulative_fee_snapshot(
        quantities=[6], prices=[100], cumulative_fee=1.2,
    )
    reconcile(client, store, ownership_proof=lambda *_: True)

    client.snapshot = lambda: _cumulative_fee_snapshot(
        quantities=[6, 4], prices=[100, 101], cumulative_fee=final_fee,
        status="FILLED_ALL",
    )
    reconcile(client, store, ownership_proof=lambda *_: True)
    reconcile(client, store, ownership_proof=lambda *_: True)

    state = store.snapshot()
    assert state.allocated_cash == pytest.approx(8996.0 - final_fee)
    assert state.strategy_equity == pytest.approx(9996.0 - final_fee)
    assert state.realized_pnl == pytest.approx(-final_fee)
    assert store.execution_summary()["total_fees"] == pytest.approx(final_fee)
    assert [row["fee"] for row in store.fills(limit=10)] == [0.0, 0.0]
    with store.connect() as con:
        assert con.execute(
            "SELECT cumulative_fee FROM order_fee_accounts"
        ).fetchone()[0] == pytest.approx(final_fee)


def test_legacy_applied_fill_migration_credits_historical_fee_once(tmp_path):
    db_path = tmp_path / "strategy.db"
    fill_hash = hashlib.sha256(b"module-deal").hexdigest()
    with sqlite3.connect(db_path) as con:
        con.execute("""CREATE TABLE applied_fills (
            fill_hash TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
            quantity REAL NOT NULL CHECK(quantity>0),
            price REAL NOT NULL CHECK(price>=0),
            fee REAL NOT NULL CHECK(fee>=0),
            applied_at TEXT NOT NULL
        )""")
        con.execute(
            "INSERT INTO applied_fills VALUES(?,?,?,?,?,?,?)",
            (fill_hash, "US.AAPL", "BUY", 2, 100, 1.0, "2026-01-01T00:00:00+00:00"),
        )

    store = LiveStrategyStore(db_path, tmp_path / "archives")
    with store.connect() as con:
        columns = {row[1]: row for row in con.execute("PRAGMA table_info(applied_fills)")}
        assert columns["fee_is_stable"][4] == "0"
        migrated = con.execute(
            "SELECT fee_is_stable,order_hash FROM applied_fills WHERE fill_hash=?", (fill_hash,),
        ).fetchone()
        assert tuple(migrated) == (0, None)
        con.execute("""INSERT INTO owned_positions
            (symbol,quantity,average_cost,market_price,market_value,realized_pnl,updated_at)
            VALUES('US.AAPL',2,100.5,100,200,0,'2026-01-01T00:00:00+00:00')""")
        con.execute("""UPDATE strategy_state SET
            lifecycle='ACTIVE',freeze_latched=0,freeze_reason=NULL,
            allocated_cash=9799,owned_market_value=200,strategy_equity=9999,
            unrealized_pnl=-1,last_sync_at='synced' WHERE id=1""")

    reconcile(FakeClient(), store, ownership_proof=lambda *_: True)
    reconcile(FakeClient(), store, ownership_proof=lambda *_: True)

    state = store.snapshot()
    assert state.allocated_cash == pytest.approx(9799)
    assert state.strategy_equity == pytest.approx(9999)
    assert store.execution_summary()["total_fees"] == pytest.approx(1.0)
    with store.connect() as con:
        adjustments = con.execute(
            "SELECT previous_total,new_total,fill_fee_credit,delta "
            "FROM order_fee_adjustments ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in adjustments] == [(0.0, 1.0, 1.0, 0.0)]
        assert con.execute("SELECT COUNT(*) FROM order_fee_accounts").fetchone()[0] == 1


def test_cumulative_order_fee_partial_to_final_is_delta_accounted_idempotently(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()

    partial = _cumulative_fee_snapshot(quantities=[4], prices=[100], cumulative_fee=1.0)
    client.snapshot = lambda: partial
    first = reconcile(client, store, ownership_proof=lambda *_: True)
    assert first["applied_fills"] == 1
    assert store.snapshot().allocated_cash == pytest.approx(9599.0)

    additional = _cumulative_fee_snapshot(
        quantities=[4, 3], prices=[100, 101], cumulative_fee=1.0,
    )
    client.snapshot = lambda: additional
    second = reconcile(client, store, ownership_proof=lambda *_: True)
    assert second["applied_fills"] == 1
    assert store.snapshot().allocated_cash == pytest.approx(9296.0)

    final = _cumulative_fee_snapshot(
        quantities=[4, 3, 3], prices=[100, 101, 102], cumulative_fee=1.5,
        status="FILLED_ALL",
    )
    client.snapshot = lambda: final
    third = reconcile(client, store, ownership_proof=lambda *_: True)
    duplicate = reconcile(client, store, ownership_proof=lambda *_: True)

    assert third["applied_fills"] == 1
    assert duplicate["applied_fills"] == 0
    assert store.snapshot().allocated_cash == pytest.approx(8989.5)
    assert store.snapshot().strategy_equity == pytest.approx(9989.5)
    assert store.execution_summary()["total_fees"] == pytest.approx(1.5)
    assert store.snapshot().realized_pnl == pytest.approx(-1.5)
    store.mark_to_market({"US.AAPL": 100}, sync_complete=False)
    assert store.snapshot().realized_pnl == pytest.approx(-1.5)
    assert [row["fee"] for row in store.fills(limit=10)] == [0.0, 0.0, 0.0]
    with store.connect() as con:
        adjustments = con.execute(
            "SELECT previous_total,new_total,delta FROM order_fee_adjustments ORDER BY id"
        ).fetchall()
        account = con.execute(
            "SELECT cumulative_fee,finalized FROM order_fee_accounts"
        ).fetchone()
    assert [tuple(row) for row in adjustments] == [
        (0.0, 1.0, 1.0),
        (1.0, 1.5, 0.5),
    ]
    assert tuple(account) == (1.5, 1)
    assert store.snapshot().lifecycle == "ACTIVE"


def test_cumulative_fee_progress_survives_restart_without_double_charge(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    partial = _cumulative_fee_snapshot(quantities=[4], prices=[100], cumulative_fee=1.0)
    client.snapshot = lambda: partial
    reconcile(client, store, ownership_proof=lambda *_: True)

    restarted = LiveStrategyStore(store.path, tmp_path / "archives")
    final = _cumulative_fee_snapshot(
        quantities=[4, 6], prices=[100, 101], cumulative_fee=1.4,
        status="FILLED_ALL",
    )
    client.snapshot = lambda: final
    reconcile(client, restarted, ownership_proof=lambda *_: True)
    reconcile(client, restarted, ownership_proof=lambda *_: True)

    assert restarted.snapshot().allocated_cash == pytest.approx(8992.6)
    assert restarted.execution_summary()["total_fees"] == pytest.approx(1.4)
    assert restarted.snapshot().freeze_reason is None


def test_fee_adjustment_and_new_fill_rollback_together_then_restart_replays_once(
    tmp_path, monkeypatch,
):
    store = active_store(tmp_path)
    client = FakeClient()
    partial = _cumulative_fee_snapshot(quantities=[4], prices=[100], cumulative_fee=1.0)
    client.snapshot = lambda: partial
    reconcile(client, store, ownership_proof=lambda *_: True)
    before_cash = store.snapshot().allocated_cash
    original_event_tx = store._event_tx

    def crash_during_adjustment(con, event_type, *args, **kwargs):
        if event_type == "order_fee_adjusted":
            raise RuntimeError("simulated crash before transaction commit")
        return original_event_tx(con, event_type, *args, **kwargs)

    final = _cumulative_fee_snapshot(
        quantities=[4, 6], prices=[100, 101], cumulative_fee=1.4,
        status="FILLED_ALL",
    )
    client.snapshot = lambda: final
    monkeypatch.setattr(store, "_event_tx", crash_during_adjustment)
    with pytest.raises(RuntimeError, match="simulated crash"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.owned_quantity("US.AAPL") == 4
    assert store.snapshot().allocated_cash == before_cash
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM applied_fills").fetchone()[0] == 1
        assert con.execute("SELECT cumulative_fee FROM order_fee_accounts").fetchone()[0] == 1.0

    restarted = LiveStrategyStore(store.path, tmp_path / "archives")
    reconcile(client, restarted, ownership_proof=lambda *_: True)
    reconcile(client, restarted, ownership_proof=lambda *_: True)
    assert restarted.owned_quantity("US.AAPL") == 10
    assert restarted.execution_summary()["total_fees"] == pytest.approx(1.4)


def test_stable_per_deal_fee_is_immutable_and_order_total_only_books_difference(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    partial = _cumulative_fee_snapshot(quantities=[4], prices=[100], cumulative_fee=1.0)
    partial["deals"][0]["deal_fee"] = 0.6
    client.snapshot = lambda: partial
    reconcile(client, store, ownership_proof=lambda *_: True)

    final = _cumulative_fee_snapshot(
        quantities=[4, 6], prices=[100, 101], cumulative_fee=1.3,
        status="FILLED_ALL",
    )
    final["deals"][0]["deal_fee"] = 0.6
    final["deals"][1]["deal_fee"] = 0.7
    client.snapshot = lambda: final
    reconcile(client, store, ownership_proof=lambda *_: True)
    reconcile(client, store, ownership_proof=lambda *_: True)

    assert sorted(row["fee"] for row in store.fills(limit=10)) == [0.6, 0.7]
    assert store.execution_summary()["total_fees"] == pytest.approx(1.3)
    assert store.snapshot().allocated_cash == pytest.approx(8992.7)
    assert store.snapshot().freeze_reason is None


def test_terminal_fill_without_any_fee_truth_is_rejected_but_partial_can_defer(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    partial = _cumulative_fee_snapshot(quantities=[4], prices=[100], cumulative_fee=0.0)
    partial["order_fees"] = []
    client.snapshot = lambda: partial
    result = reconcile(client, store, ownership_proof=lambda *_: True)
    assert result["applied_fills"] == 1
    assert store.execution_summary()["total_fees"] == 0

    final = _cumulative_fee_snapshot(
        quantities=[4, 6], prices=[100, 101], cumulative_fee=0.0,
        status="FILLED_ALL",
    )
    final["order_fees"] = []
    client.snapshot = lambda: final
    with pytest.raises(ControlRejected, match="fee record missing"):
        reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.owned_quantity("US.AAPL") == 4


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


@pytest.mark.parametrize("mutation", [
    lambda data: data["orders"][0].update(qty=float("nan")),
    lambda data: data["orders"][0].update(qty=-1),
    lambda data: data["orders"][0].update(dealt_qty=-1),
    lambda data: data["orders"][0].update(dealt_qty=float("nan")),
    lambda data: data["orders"][0].update(qty=1, dealt_qty=2),
    lambda data: data["orders"][0].update(price=float("inf")),
    lambda data: data["deals"][0].update(deal_qty=-1),
    lambda data: data["deals"][0].update(deal_price=float("nan")),
    lambda data: data["deals"][0].update(deal_fee=float("inf")),
    lambda data: data["deals"][0].update(deal_fee=-1),
    lambda data: data["order_fees"][0].update(fee_amount=float("nan")),
    lambda data: data["order_fees"][0].update(fee_amount=-1),
])
def test_module_owned_invalid_economics_latch_permanent_freeze(tmp_path, mutation):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    mutation(data)
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="invalid|negative|contradictory"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    state = store.snapshot()
    assert state.lifecycle == "FROZEN"
    assert state.freeze_reason == "reconciliation_snapshot_numeric_conflict"
    event = store.recent_events(1)[0]
    assert event["event_type"] == "reconciliation_snapshot_numeric_conflict"
    assert set(event["details"]) == {"symbol", "conflicting_fields"}
    assert store.positions() == []


def test_invalid_economics_replaces_only_recoverable_auto_freeze(tmp_path):
    store = active_store(tmp_path)
    with store.connect() as con:
        con.execute(
            "UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
            "freeze_reason='auto_post_broker_reconciliation_failed' WHERE id=1"
        )
    client = FakeClient()
    data = client.snapshot()
    data["order_fees"][0]["fee_amount"] = float("nan")
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="invalid"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.snapshot().freeze_reason == "reconciliation_snapshot_numeric_conflict"


@pytest.mark.parametrize("target,aliases", [
    ("order_fees", {"fee_amount": 1, "total_fee": 2.0}),
    ("order_fees", {"fee_amount": 1, "commission": 2.0}),
    ("deals", {"deal_fee": 1, "fee_amount": 2.0}),
    ("deals", {"deal_fee": 1, "commission": 2.0}),
])
def test_module_fee_alias_conflict_latches_permanent_freeze(tmp_path, target, aliases):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data[target][0].update(aliases)
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="conflicting"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.snapshot().freeze_reason == "reconciliation_snapshot_numeric_conflict"
    assert store.recent_events(1)[0]["details"]["conflicting_fields"] == ["fee"]


def test_equal_module_fee_aliases_are_canonicalized(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    data = client.snapshot()
    data["order_fees"][0].update(total_fee=1, fee_amount=1.0, commission=1.0)
    data["deals"][0].update(deal_fee=1, fee_amount=1.0, commission=1.0)
    client.snapshot = lambda: data

    result = reconcile(client, store, ownership_proof=lambda *_: True)

    assert result["applied_fills"] == 1
    assert store.execution_summary()["total_fees"] == pytest.approx(1.0)


def test_shared_restricted_ignores_invalid_fee_on_unrelated_manual_order(tmp_path):
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
    data["order_fees"].append({"order_id": "manual-order", "fee_amount": float("nan")})
    data["positions"].append({"code": "US.MSFT", "qty": 50})
    client.snapshot = lambda: data

    result = reconcile(client, store, ownership_proof=lambda *_: True)

    assert result["applied_fills"] == 1
    assert store.snapshot().lifecycle == "ACTIVE"


def _buy_then_partial_sell_snapshot(*, buy_fee_known: bool, sell_fee_known: bool = True) -> dict:
    data = FakeClient().snapshot()
    buy = data["orders"][0]
    buy_deal = data["deals"][0]
    if buy_fee_known:
        buy_deal["deal_fee"] = 0.6
    sell = {
        "order_id": "module-sell", "code": "US.AAPL", "trd_side": "SELL",
        "order_status": "FILLED_ALL", "qty": 1, "dealt_qty": 1, "price": 110,
        "remark": "dashboard:B16:sell-preview",
    }
    sell_deal = {
        "deal_id": "module-sell-deal", "order_id": "module-sell", "code": "US.AAPL",
        "trd_side": "SELL", "deal_qty": 1, "deal_price": 110,
    }
    if sell_fee_known:
        sell_deal["deal_fee"] = 0.2
    data.update(
        orders=[buy, sell],
        deals=[buy_deal, sell_deal],
        order_fees=[
            {"order_id": "module-order", "fee_amount": 1.0},
            {"order_id": "module-sell", "fee_amount": 0.5},
        ],
        positions=[{"code": "US.AAPL", "qty": 1}],
    )
    return data


def _run_buy_fee_path(tmp_path, *, delayed: bool):
    store = active_store(tmp_path)
    client = FakeClient()
    first = client.snapshot()
    if not delayed:
        first["deals"][0]["deal_fee"] = 0.6
    client.snapshot = lambda: first
    reconcile(client, store, ownership_proof=lambda *_: True)

    sold = _buy_then_partial_sell_snapshot(buy_fee_known=not delayed)
    client.snapshot = lambda: sold
    reconcile(client, store, ownership_proof=lambda *_: True)
    if delayed:
        final = _buy_then_partial_sell_snapshot(buy_fee_known=True)
        client.snapshot = lambda: final
        reconcile(client, store, ownership_proof=lambda *_: True)
        reconcile(client, store, ownership_proof=lambda *_: True)
    position = store.positions()[0]
    state = store.snapshot()
    return position, state, store.execution_summary()


def test_delayed_stable_buy_fee_matches_immediate_path_after_partial_sell(tmp_path):
    immediate = _run_buy_fee_path(tmp_path / "immediate", delayed=False)
    delayed = _run_buy_fee_path(tmp_path / "delayed", delayed=True)

    immediate_position, immediate_state, immediate_execution = immediate
    delayed_position, delayed_state, delayed_execution = delayed
    for field in ("quantity", "average_cost", "realized_pnl", "market_value"):
        assert delayed_position[field] == pytest.approx(immediate_position[field])
    for field in (
        "allocated_cash", "strategy_equity", "realized_pnl", "unrealized_pnl",
    ):
        assert getattr(delayed_state, field) == pytest.approx(getattr(immediate_state, field))
    assert delayed_execution["total_fees"] == pytest.approx(immediate_execution["total_fees"])


def _run_sell_fee_path(tmp_path, *, delayed: bool):
    store = active_store(tmp_path)
    client = FakeClient()
    first = client.snapshot()
    first["deals"][0]["deal_fee"] = 0.6
    client.snapshot = lambda: first
    reconcile(client, store, ownership_proof=lambda *_: True)

    sold = _buy_then_partial_sell_snapshot(
        buy_fee_known=True, sell_fee_known=not delayed,
    )
    client.snapshot = lambda: sold
    reconcile(client, store, ownership_proof=lambda *_: True)
    if delayed:
        final = _buy_then_partial_sell_snapshot(
            buy_fee_known=True, sell_fee_known=True,
        )
        client.snapshot = lambda: final
        reconcile(client, store, ownership_proof=lambda *_: True)
        reconcile(client, store, ownership_proof=lambda *_: True)
    return store.positions()[0], store.snapshot(), store.execution_summary()


def test_delayed_stable_sell_fee_matches_immediate_path(tmp_path):
    immediate = _run_sell_fee_path(tmp_path / "immediate", delayed=False)
    delayed = _run_sell_fee_path(tmp_path / "delayed", delayed=True)

    for field in ("quantity", "average_cost", "realized_pnl", "market_value"):
        assert delayed[0][field] == pytest.approx(immediate[0][field])
    for field in (
        "allocated_cash", "strategy_equity", "realized_pnl", "unrealized_pnl",
    ):
        assert getattr(delayed[1], field) == pytest.approx(getattr(immediate[1], field))
    assert delayed[2]["total_fees"] == pytest.approx(immediate[2]["total_fees"])


@pytest.mark.parametrize("mutation,field", [
    (lambda order: order.update(qty=float("nan")), "quantity"),
    (lambda order: order.update(qty=float("inf")), "quantity"),
    (lambda order: order.update(qty=-1), "quantity"),
    (lambda order: order.update(qty=3), "quantity"),
    (lambda order: order.update(price=101), "price"),
])
def test_real_default_proof_latches_bound_order_economic_corruption(
    tmp_path, monkeypatch, mutation, field,
):
    store = active_store(tmp_path)
    store.observe_runtime_fingerprint("test-sync-fingerprint")
    store.record_broker_sync_proof("test-sync-fingerprint", "synced")
    bind_fake_order_to_real_preview(tmp_path, monkeypatch)
    client = FakeClient()
    data = client.snapshot()
    mutation(data["orders"][0])
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="invalid|contradict|differs") as rejected:
        reconcile(client, store)

    assert "ownership forgery" not in str(rejected.value)
    assert store.snapshot().freeze_reason in {
        "reconciliation_snapshot_numeric_conflict",
        "reconciliation_snapshot_order_conflict",
    }
    event = store.recent_events(1)[0]
    assert field in event["details"]["conflicting_fields"]
    assert set(event["details"]) == {"symbol", "conflicting_fields"}
    assert store.positions() == []
    assert not store.broker_sync_proof_matches("test-sync-fingerprint")


@pytest.mark.parametrize("reconcile_status", [False, True])
@pytest.mark.parametrize("bad_quantity", [float("nan"), -1, 1])
def test_claimed_preview_economic_corruption_permanently_latches(
    tmp_path, monkeypatch, reconcile_status, bad_quantity,
):
    store = active_store(tmp_path)
    store.observe_runtime_fingerprint("test-sync-fingerprint")
    store.record_broker_sync_proof("test-sync-fingerprint", "synced")
    bind_claimed_preview(tmp_path, monkeypatch, reconcile_status=reconcile_status)
    client = FakeClient()
    data = client.snapshot()
    data["orders"][0]["qty"] = bad_quantity
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="invalid|differs|contradictory") as rejected:
        reconcile(client, store)

    assert "ownership forgery" not in str(rejected.value)
    assert store.snapshot().freeze_reason in {
        "reconciliation_snapshot_numeric_conflict",
        "reconciliation_snapshot_order_conflict",
    }
    assert not store.broker_sync_proof_matches("test-sync-fingerprint")


@pytest.mark.parametrize("field,bad", [("code", ""), ("trd_side", "HOLD")])
def test_real_default_proof_latches_bound_order_invalid_identity_even_without_deals(
    tmp_path, monkeypatch, field, bad,
):
    store = active_store(tmp_path)
    bind_fake_order_to_real_preview(tmp_path, monkeypatch)
    client = FakeClient()
    data = client.snapshot()
    data["orders"][0].update(order_status="CANCELLED_ALL", dealt_qty=0)
    data["orders"][0][field] = bad
    data.update(deals=[], order_fees=[], positions=[])
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="symbol|side"):
        reconcile(client, store)

    assert store.snapshot().freeze_reason == "reconciliation_snapshot_order_conflict"
    assert store.recent_events(1)[0]["details"]["conflicting_fields"] == [
        "symbol" if field == "code" else "side"
    ]


@pytest.mark.parametrize("mutation,expected_field", [
    (lambda deal: deal.pop("deal_id"), "deal_identity"),
    (lambda deal: deal.update(trd_side="HOLD"), "side"),
])
def test_proven_module_deal_invalid_identity_latches_and_blocks_sync_proof(
    tmp_path, monkeypatch, mutation, expected_field,
):
    store = active_store(tmp_path)
    bind_fake_order_to_real_preview(tmp_path, monkeypatch)
    client = FakeClient()
    data = client.snapshot()
    mutation(data["deals"][0])
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="deal|side|identity"):
        reconcile(client, store)

    state = store.snapshot()
    assert state.lifecycle == "FROZEN"
    assert state.freeze_reason == "reconciliation_snapshot_order_conflict"
    event = store.recent_events(1)[0]
    assert expected_field in event["details"]["conflicting_fields"]
    assert not store.broker_sync_proof_matches("test-sync-fingerprint")


@pytest.mark.parametrize("positions,field", [
    ([{"code": "US.MSFT", "qty": float("nan")}], "quantity"),
    ([{"code": "US.MSFT", "qty": float("inf")}], "quantity"),
    ([{"code": "US.MSFT", "qty": "invalid"}], "quantity"),
    ([{"code": "US.MSFT", "qty": -1}], "quantity"),
    ([{"code": "", "qty": 1}], "symbol"),
    ([{"code": "US.MSFT", "qty": 1, "position_side": "SHORT"}], "side"),
    ([{"code": "US.MSFT", "qty": 1}, {"code": "us.msft", "qty": 2}], "quantity"),
])
def test_dedicated_untrusted_position_snapshot_permanently_latches(
    tmp_path, positions, field,
):
    store = active_store(tmp_path)
    store.observe_runtime_fingerprint("test-sync-fingerprint")
    store.record_broker_sync_proof("test-sync-fingerprint", "synced")
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode="DEDICATED", dedicated_account_confirmed=True,
        shared_account_risk_accepted=False, trading_enabled=False,
        auto_trading_enabled=False, trade_api_token="", password_md5="",
    )
    data = client.snapshot()
    data.update(orders=[], deals=[], order_fees=[], positions=positions)
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="position"):
        reconcile(client, store)

    assert store.snapshot().freeze_reason == "reconciliation_snapshot_position_conflict"
    event = store.recent_events(1)[0]
    assert event["event_type"] == "reconciliation_snapshot_position_conflict"
    assert field in event["details"]["conflicting_fields"]
    assert store.positions() == []
    assert not store.broker_sync_proof_matches("test-sync-fingerprint")


@pytest.mark.parametrize("mode", ["DEDICATED", "SHARED_RESTRICTED"])
def test_duplicate_in_scope_position_rows_permanently_latch(tmp_path, mode):
    store = active_store(tmp_path)
    store.observe_runtime_fingerprint("test-sync-fingerprint")
    store.record_broker_sync_proof("test-sync-fingerprint", "synced")
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode=mode,
        dedicated_account_confirmed=mode == "DEDICATED",
        shared_account_risk_accepted=mode == "SHARED_RESTRICTED",
        trading_enabled=mode == "SHARED_RESTRICTED",
        auto_trading_enabled=False, trade_api_token="", password_md5="",
    )
    data = client.snapshot()
    data["positions"].append(dict(data["positions"][0], code="us.aapl", qty=2.0))
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="duplicate"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert store.snapshot().freeze_reason == "reconciliation_snapshot_position_conflict"
    assert not store.broker_sync_proof_matches("test-sync-fingerprint")


def test_shared_external_positions_skip_untrusted_economics_and_duplicates(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode="SHARED_RESTRICTED", dedicated_account_confirmed=False,
        shared_account_risk_accepted=True, trading_enabled=True,
        auto_trading_enabled=False, trade_api_token="", password_md5="",
    )
    data = client.snapshot()
    data["positions"].extend([
        {"code": "US.MSFT", "qty": "not-sensitive-economics", "position_side": "SHORT"},
        {"code": "us.msft", "qty": float("nan")},
    ])
    client.snapshot = lambda: data

    result = reconcile(client, store, ownership_proof=lambda *_: True)

    assert result["applied_fills"] == 1
    assert result["external_positions"] == 1
    assert store.owned_quantity("US.AAPL") == 2


@pytest.mark.parametrize("bad_symbol", ["", "not a symbol", "../ACCT-SECRET"])
def test_shared_unrecognizable_position_symbol_permanently_latches(tmp_path, bad_symbol):
    store = active_store(tmp_path)
    client = FakeClient()
    client.settings = SimpleNamespace(
        account_mode="SHARED_RESTRICTED", dedicated_account_confirmed=False,
        shared_account_risk_accepted=True, trading_enabled=True,
        auto_trading_enabled=False, trade_api_token="", password_md5="",
    )
    data = client.snapshot()
    data.update(orders=[], deals=[], order_fees=[], positions=[{"code": bad_symbol, "qty": "bad"}])
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="symbol"):
        reconcile(client, store)

    assert store.snapshot().freeze_reason == "reconciliation_snapshot_position_conflict"


def test_quantity_mismatch_invalidates_previously_successful_sync_proof(tmp_path):
    store = active_store(tmp_path)
    client = FakeClient()
    reconcile(client, store, ownership_proof=lambda *_: True)
    assert store.broker_sync_proof_matches("test-sync-fingerprint")
    data = client.snapshot()
    data["positions"][0]["qty"] = 1
    client.snapshot = lambda: data

    with pytest.raises(ControlRejected, match="staged strategy quantity"):
        reconcile(client, store, ownership_proof=lambda *_: True)

    assert not store.broker_sync_proof_matches("test-sync-fingerprint")


def test_generic_sync_failure_never_persists_or_outputs_raw_exception_secrets(
    tmp_path, monkeypatch, capsys,
):
    store = active_store(tmp_path)
    malicious = (
        "token abcSUPERSECRET order_id ABC-ORDER-9988 deal reference DEAL-778899 "
        "account 123456789 broker reference BRK-SECRET-445566 "
        "Authorization: Bearer bearer-secret-abcdef0123456789 "
        "opaque_ZYXWVUTSRQPONMLK987654321"
    )
    monkeypatch.setattr(_module, "LiveStrategyStore", lambda: store)
    monkeypatch.setattr(_module, "MoomooClient", lambda control_store: object())
    monkeypatch.setattr(
        _module, "reconcile",
        lambda *_: (_ for _ in ()).throw(RuntimeError(malicious)),
    )
    stream = io.StringIO()
    test_logger = logging.Logger("sync-secret-probe")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    test_logger.addHandler(handler)
    monkeypatch.setattr(_module, "logger", test_logger)
    monkeypatch.setattr(sys, "argv", ["live_account_sync.py"])

    assert _module.main() == 2

    combined = "\n".join([
        capsys.readouterr().out,
        stream.getvalue(),
        json.dumps(store.recent_events(10), sort_keys=True),
    ])
    for secret in (
        "abcSUPERSECRET", "ABC-ORDER-9988", "DEAL-778899", "123456789",
        "BRK-SECRET-445566", "bearer-secret-abcdef0123456789",
        "opaque_ZYXWVUTSRQPONMLK987654321",
    ):
        assert secret not in combined
    assert "RECONCILIATION_INTERNAL_ERROR" in combined
