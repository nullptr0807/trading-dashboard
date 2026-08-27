from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

import core.live_auto_executor as module
from core.live_auto_executor import AutoExecutionError, LiveAutoExecutor, recover_auto_intents
from core.live_signal_adapter import RankedSignal, SignalBatch
from core.live_strategy_control import ControlRejected, LiveStrategyStore
from core.moomoo_client import BrokerOutcomeUnknown


NOW = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)


def signal_batch():
    symbols = [f"S{i:02d}" for i in range(30)] + ["DRAM"]
    ranking = tuple(RankedSignal(symbol, 1 - i / 100) for i, symbol in enumerate(symbols))
    return SignalBatch(
        "B16", "2026-08-25", ("factor",), "f" * 64, "b" * 64,
        ranking, tuple(symbols[:-4]), tuple(symbols[-4:]),
    )


class FakeClient:
    def __init__(self):
        self.settings = SimpleNamespace(
            trading_enabled=True, auto_trading_enabled=True, trade_api_token="trade-token",
        )
        self.orders = []
        self.previews = []

    @staticmethod
    def normalize_code(symbol):
        value = str(symbol).upper()
        return value if value.startswith("US.") else "US." + value

    def quote(self, symbol):
        return {"code": self.normalize_code(symbol), "last_price": 100.0,
                "bid_price": 99.0, "ask_price": 101.0,
                "market_state": "MORNING", "sec_status": "NORMAL"}

    def quotes(self, symbols):
        return {self.normalize_code(symbol): self.quote(symbol) for symbol in symbols}

    def snapshot(self):
        return {"orders": list(self.orders), "positions": [], "deals": [], "account": {}}

    def preview_order(self, **kwargs):
        self.previews.append(kwargs)
        return {**kwargs, "preview_id": "preview-safe", "preview_token": "signed"}


def active_store(tmp_path):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    store.apply_fill("verified", "US.DRAM", "BUY", 1, 100)
    with store.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                    "freeze_reason=NULL,last_sync_at=? WHERE id=1", (NOW.isoformat(),))
    return store


def executor(tmp_path, reconcile=lambda client, store: {"ok": True}):
    client = FakeClient()
    store = active_store(tmp_path)
    result = LiveAutoExecutor(
        cast(Any, client), store, signal_loader=lambda **kwargs: signal_batch(), reconcile_fn=reconcile,
    )
    return result, client, store


def test_shadow_is_zero_mutation_and_uses_live_owned_position(tmp_path):
    ex, _, store = executor(tmp_path)
    result = ex.shadow(now=NOW)
    assert result["broker_mutation"] is False
    assert result["intents"][0] == {
        "symbol": "US.DRAM", "side": "SELL", "quantity": 1,
        "limit_price": 99.0, "purpose": "RANK_EXIT",
        "order_type": "LIMIT", "time_in_force": "DAY", "session": "RTH",
    }
    assert store.list_auto_order_intents() == []


def test_execute_outside_rth_creates_no_intent(tmp_path):
    ex, _, store = executor(tmp_path)
    with pytest.raises(AutoExecutionError, match="RTH"):
        ex.execute_one(now=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc))
    assert store.list_auto_order_intents() == []


def test_execute_dispatches_one_intent_and_marks_filled(tmp_path, monkeypatch):
    ex, client, store = executor(tmp_path)
    calls = []

    def dispatch(_client, _preview, _token, *, source):
        calls.append(source)
        return {"accepted": True, "order": {"order_id": "raw-id", "order_status": "FILLED_ALL"}}

    monkeypatch.setattr(module, "dispatch_signed_preview", dispatch)
    result = ex.execute_one(now=NOW)
    assert calls == ["auto_executor"]
    assert result["side"] == "SELL"
    assert result["quantity"] == 1
    assert "raw-id" not in repr(result)
    assert store.list_auto_order_intents()[0]["status"] == "FILLED"
    assert client.previews[0]["auto_intent_id"]


def test_broker_unknown_freezes_and_never_makes_intent_retryable(tmp_path, monkeypatch):
    ex, _, store = executor(tmp_path)
    monkeypatch.setattr(
        module, "dispatch_signed_preview",
        lambda *args, **kwargs: (_ for _ in ()).throw(BrokerOutcomeUnknown("timeout")),
    )
    with pytest.raises(BrokerOutcomeUnknown):
        ex.execute_one(now=NOW)
    intent = store.list_auto_order_intents()[0]
    assert intent["status"] == "UNKNOWN"
    assert store.snapshot().lifecycle == "FROZEN"


def test_post_broker_reconciliation_failure_freezes_without_failed_retry(tmp_path, monkeypatch):
    count = 0

    def reconcile(_client, _store):
        nonlocal count
        count += 1
        if count == 2:
            raise ControlRejected("post broker sync failed")
        return {"ok": True}

    ex, _, store = executor(tmp_path, reconcile)
    monkeypatch.setattr(
        module, "dispatch_signed_preview",
        lambda *args, **kwargs: {"accepted": True, "order": {"order_id": "raw", "order_status": "SUBMITTED"}},
    )
    with pytest.raises(ControlRejected):
        ex.execute_one(now=NOW)
    intent = store.list_auto_order_intents()[0]
    assert intent["status"] == "ACKED"
    assert intent["reserved_sell_qty"] == 1
    assert store.snapshot().lifecycle == "FROZEN"


def test_recovery_missing_dispatched_broker_order_becomes_unknown_and_frozen(tmp_path):
    store = active_store(tmp_path)
    intent = store.create_auto_order_intent(
        strategy_id="B16", config_version=1, signal_batch_id="b" * 64,
        signal_source_date="2026-08-25", factor_set_hash="f" * 64,
        symbol="US.DRAM", side="SELL", purpose="RANK_EXIT", target_qty=0,
        order_qty=1, limit_price=99,
    )
    store.mark_auto_intent_dispatching(intent["intent_id"], "missing-preview")
    blocker = recover_auto_intents(store, {"orders": []})
    assert blocker is not None
    assert blocker["status"] == "UNKNOWN"
    assert store.snapshot().freeze_reason == "auto_intent_broker_outcome_unknown"


def test_stale_reserved_config_is_cancelled_before_dispatch(tmp_path, monkeypatch):
    ex, _, store = executor(tmp_path)
    old = store.create_auto_order_intent(
        strategy_id="B16", config_version=1, signal_batch_id="b" * 64,
        signal_source_date="2026-08-25", factor_set_hash="f" * 64,
        symbol="US.DRAM", side="SELL", purpose="RANK_EXIT", target_qty=0,
        order_qty=1, limit_price=99,
    )
    store.update_config({"top_n": 5}, 1, "test", "new live config")
    with store.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                    "freeze_reason=NULL,last_sync_at=? WHERE id=1", (NOW.isoformat(),))
    monkeypatch.setattr(
        module, "dispatch_signed_preview",
        lambda *args, **kwargs: {"accepted": True, "order": {
            "order_id": "new-config-order", "order_status": "FILLED_ALL",
        }},
    )
    result = ex.execute_one(now=NOW)
    assert result["status"] == "broker_accepted"
    intents = store.list_auto_order_intents()
    old_after = store.get_auto_order_intent(old["intent_id"])
    assert old_after is not None and old_after["status"] == "CANCELLED"
    assert any(row["config_version"] == 2 and row["status"] == "FILLED" for row in intents)


def test_broker_visible_ack_handoffs_local_reservation(tmp_path):
    store = active_store(tmp_path)
    intent = store.create_auto_order_intent(
        strategy_id="B16", config_version=1, signal_batch_id="b" * 64,
        signal_source_date="2026-08-25", factor_set_hash="f" * 64,
        symbol="US.DRAM", side="SELL", purpose="RANK_EXIT", target_qty=0,
        order_qty=1, limit_price=99,
    )
    store.mark_auto_intent_dispatching(intent["intent_id"], "visible-preview")
    store.mark_auto_intent_acked(intent["intent_id"])
    blocker = recover_auto_intents(store, {"orders": [{
        "remark": "dashboard:B16:visible-preview", "order_status": "SUBMITTED",
        "code": "US.DRAM", "trd_side": "SELL", "qty": 1, "dealt_qty": 0,
        "price": 99,
    }]})
    assert blocker is not None and blocker["status"] == "ACKED"
    assert blocker["reserved_sell_qty"] == 0


def test_mismatched_broker_order_never_releases_local_reservation(tmp_path):
    store = active_store(tmp_path)
    intent = store.create_auto_order_intent(
        strategy_id="B16", config_version=1, signal_batch_id="b" * 64,
        signal_source_date="2026-08-25", factor_set_hash="f" * 64,
        symbol="US.DRAM", side="SELL", purpose="RANK_EXIT", target_qty=0,
        order_qty=1, limit_price=99,
    )
    store.mark_auto_intent_dispatching(intent["intent_id"], "mismatch-preview")
    store.mark_auto_intent_acked(intent["intent_id"])
    blocker = recover_auto_intents(store, {"orders": [{
        "remark": "dashboard:B16:mismatch-preview", "order_status": "SUBMITTED",
        "code": "US.WRONG", "trd_side": "BUY", "qty": 99, "dealt_qty": 0,
        "price": 1,
    }]})
    assert blocker is not None and blocker["status"] == "UNKNOWN"
    assert blocker["reserved_sell_qty"] == 1
    assert store.snapshot().lifecycle == "FROZEN"
