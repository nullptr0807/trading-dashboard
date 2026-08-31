from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

import core.live_auto_executor as module
from core.live_auto_executor import AutoExecutionError, LiveAutoExecutor, recover_auto_intents
from core.live_signal_adapter import RankedSignal, SignalBatch
from core.live_strategy_control import ControlRejected, LiveStrategyStore
from core.moomoo_client import BrokerOutcomeUnknown, MoomooUnavailable


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
        # Keep the fixed-clock tests deterministic even when run later on NOW's date.
        con.execute("UPDATE applied_fills SET applied_at=?", ((NOW.replace(hour=13)).isoformat(),))
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


def test_dispatch_fill_response_stays_acked_until_reconciler_observes_fill(tmp_path, monkeypatch):
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
    # The placement response can lead order/deal/position visibility. It is not
    # sufficient evidence to release the global unresolved-intent blocker.
    assert store.list_auto_order_intents()[0]["status"] == "ACKED"
    assert client.previews[0]["auto_intent_id"]
    with pytest.raises(ControlRejected, match="unresolved"):
        store.create_auto_order_intent(
            strategy_id="B16", config_version=1, signal_batch_id="c" * 64,
            signal_source_date="2026-08-25", factor_set_hash="f" * 64,
            symbol="US.MSFT", side="BUY", purpose="TARGET_BUY", target_qty=1,
            order_qty=1, limit_price=100,
        )


def test_execute_serial_replans_after_each_proven_fill_and_respects_hard_cap(
        tmp_path, monkeypatch):
    reconcile_calls = 0

    def reconcile(_client, reconcile_store):
        nonlocal reconcile_calls
        reconcile_calls += 1
        for intent in reconcile_store.list_auto_order_intents(limit=100):
            if intent["status"] != "ACKED":
                continue
            reconcile_store.apply_fill(
                "fill-" + str(intent["intent_id"]), intent["symbol"], intent["side"],
                intent["order_qty"], intent["limit_price"],
            )
            reconcile_store.mark_auto_intent_filled(intent["intent_id"])
        return {"ok": True}

    ex, _, store = executor(tmp_path, reconcile)
    order_count = 0

    def dispatch(*args, **kwargs):
        nonlocal order_count
        order_count += 1
        return {"accepted": True, "order": {
            "order_id": f"serial-{order_count}", "order_status": "FILLED_ALL",
        }}

    monkeypatch.setattr(module, "dispatch_signed_preview", dispatch)

    result = ex.execute_serial(now=NOW, max_actions=2)

    assert result["status"] == "max_actions_reached"
    assert result["action_count"] == 2
    assert [row["intent_status"] for row in result["actions"]] == ["FILLED", "FILLED"]
    assert order_count == 2
    assert reconcile_calls == 4
    assert "serial-1" not in repr(result) and "serial-2" not in repr(result)
    assert store.list_auto_order_intents()[0]["status"] == "FILLED"
    assert store.list_auto_order_intents()[1]["status"] == "FILLED"


def test_execute_serial_stops_before_second_dispatch_if_hold_appears_after_fill(
        tmp_path, monkeypatch):
    reconcile_calls = 0

    def reconcile(_client, reconcile_store):
        nonlocal reconcile_calls
        reconcile_calls += 1
        for intent in reconcile_store.list_auto_order_intents(limit=100):
            if intent["status"] == "ACKED":
                reconcile_store.apply_fill(
                    "held-fill-" + str(intent["intent_id"]), intent["symbol"],
                    intent["side"], intent["order_qty"], intent["limit_price"],
                )
                reconcile_store.mark_auto_intent_filled(intent["intent_id"])
                reconcile_store.create_execution_hold(
                    "SYSTEM", "*", "POST_FILL_SAFETY_HOLD", "test_reconciler"
                )
        return {"ok": True}

    ex, _, _ = executor(tmp_path, reconcile)
    dispatch_calls = 0

    def dispatch(*args, **kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1
        return {"accepted": True, "order": {
            "order_id": "one-before-hold", "order_status": "FILLED_ALL",
        }}

    monkeypatch.setattr(module, "dispatch_signed_preview", dispatch)

    with pytest.raises(ControlRejected, match="hold blocks"):
        ex.execute_serial(now=NOW, max_actions=5)

    assert dispatch_calls == 1


def test_execute_serial_propagates_second_action_rth_close(tmp_path, monkeypatch):
    ex, _, _ = executor(tmp_path)
    calls = 0

    def crosses_close(*, now=None):
        nonlocal calls
        calls += 1
        assert now is None
        if calls == 1:
            return {"mode": "execute", "status": "broker_accepted", "intent_status": "FILLED"}
        raise AutoExecutionError("Live auto execution is restricted to US RTH")

    monkeypatch.setattr(ex, "execute_one", crosses_close)

    with pytest.raises(AutoExecutionError, match="RTH"):
        ex.execute_serial(max_actions=5)
    assert calls == 2


def test_execute_serial_hard_cap_stops_exactly_before_twenty_first_action(
        tmp_path, monkeypatch):
    ex, _, _ = executor(tmp_path)
    calls = 0

    def filled(*, now=None):
        nonlocal calls
        calls += 1
        return {"mode": "execute", "status": "broker_accepted", "intent_status": "FILLED"}

    monkeypatch.setattr(ex, "execute_one", filled)
    result = ex.execute_serial(now=NOW)

    assert result["status"] == "max_actions_reached"
    assert result["action_count"] == 20
    assert calls == 20


def test_execute_serial_fails_closed_if_intent_disappears_after_broker_acceptance(
        tmp_path, monkeypatch):
    reconcile_calls = 0

    def reconcile(_client, reconcile_store):
        nonlocal reconcile_calls
        reconcile_calls += 1
        if reconcile_calls == 2:
            with reconcile_store.connect() as con:
                con.execute("DELETE FROM auto_order_intents")
        return {"ok": True}

    ex, _, store = executor(tmp_path, reconcile)
    dispatch_calls = 0

    def dispatch(*args, **kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1
        return {"accepted": True, "order": {
            "order_id": "accepted-before-loss", "order_status": "FILLED_ALL",
        }}

    monkeypatch.setattr(module, "dispatch_signed_preview", dispatch)

    with pytest.raises(ControlRejected, match="intent state disappeared"):
        ex.execute_serial(now=NOW, max_actions=5)

    assert dispatch_calls == 1
    assert store.list_auto_order_intents() == []
    assert any(
        hold["reason_code"] == "AUTO_INTENT_STATE_MISSING"
        and hold["scope_type"] == "SYSTEM"
        for hold in store.list_execution_holds(active_only=True)
    )


def test_execute_serial_stops_when_first_order_is_not_terminal(tmp_path, monkeypatch):
    ex, _, store = executor(tmp_path)
    monkeypatch.setattr(
        module, "dispatch_signed_preview",
        lambda *args, **kwargs: {"accepted": True, "order": {
            "order_id": "still-open", "order_status": "SUBMITTED",
        }},
    )

    result = ex.execute_serial(now=NOW, max_actions=5)

    assert result["status"] == "stopped_on_nonfilled_intent"
    assert result["action_count"] == 1
    assert result["actions"][0]["intent_status"] == "ACKED"
    assert len(store.list_auto_order_intents()) == 1


@pytest.mark.parametrize("intent_status", [
    "ACKED", "PARTIAL", "CANCELLED", "FAILED", "UNKNOWN",
])
def test_execute_serial_never_continues_after_nonfilled_status(
        tmp_path, monkeypatch, intent_status):
    ex, _, _ = executor(tmp_path)
    calls = 0

    def nonfilled(*, now=None):
        nonlocal calls
        calls += 1
        return {
            "mode": "execute", "status": "broker_accepted",
            "intent_status": intent_status,
        }

    monkeypatch.setattr(ex, "execute_one", nonfilled)
    result = ex.execute_serial(now=NOW, max_actions=5)

    assert result["status"] == "stopped_on_nonfilled_intent"
    assert result["action_count"] == 1
    assert result["stop_reason"] == intent_status
    assert calls == 1


@pytest.mark.parametrize("limit", [0, 21])
def test_execute_serial_rejects_unsafe_action_limits(tmp_path, limit):
    ex, _, _ = executor(tmp_path)
    with pytest.raises(AutoExecutionError, match="between 1 and 20"):
        ex.execute_serial(now=NOW, max_actions=limit)


def test_execute_serial_uses_fresh_clock_for_production_calls(tmp_path, monkeypatch):
    ex, _, _ = executor(tmp_path)
    observed = []

    def no_action(*, now=None):
        observed.append(now)
        return {"mode": "execute", "status": "no_action"}

    monkeypatch.setattr(ex, "execute_one", no_action)
    result = ex.execute_serial()

    assert result["status"] == "no_action"
    assert observed == [None]


def test_broker_unknown_holds_intent_and_never_makes_it_retryable(tmp_path, monkeypatch):
    ex, _, store = executor(tmp_path)
    monkeypatch.setattr(
        module, "dispatch_signed_preview",
        lambda *args, **kwargs: (_ for _ in ()).throw(BrokerOutcomeUnknown("timeout")),
    )
    with pytest.raises(BrokerOutcomeUnknown):
        ex.execute_one(now=NOW)
    intent = store.list_auto_order_intents()[0]
    assert intent["status"] == "UNKNOWN"
    assert store.snapshot().lifecycle == "ACTIVE"
    assert store.applicable_execution_holds(intent_id=intent["intent_id"])[0]["reason_code"] == "BROKER_OUTCOME_UNKNOWN"


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
    assert store.snapshot().lifecycle == "ACTIVE"
    assert store.applicable_execution_holds(intent_id=intent["intent_id"])[0]["reason_code"] == "POST_BROKER_RECONCILIATION_FAILED"


@pytest.mark.parametrize("latched_reason", [
    "reconciliation_quantity_mismatch",
    "reconciliation_snapshot_deal_conflict",
])
def test_post_broker_reconciliation_latch_is_never_overwritten(
    tmp_path, monkeypatch, latched_reason,
):
    count = 0

    def reconcile(_client, reconcile_store):
        nonlocal count
        count += 1
        if count == 2:
            reconcile_store.create_execution_hold(
                "SYMBOL", "US.DRAM", latched_reason.upper(), "moomoo_reconciler"
            )
            raise ControlRejected("Broker reconciliation conflict")
        return {"ok": True}

    ex, _, store = executor(tmp_path, reconcile)
    monkeypatch.setattr(
        module, "dispatch_signed_preview",
        lambda *args, **kwargs: {"accepted": True, "order": {
            "order_id": "raw", "order_status": "SUBMITTED",
        }},
    )

    with pytest.raises(ControlRejected, match="reconciliation conflict"):
        ex.execute_one(now=NOW)

    reasons = {h["reason_code"] for h in store.list_execution_holds(active_only=True)}
    assert latched_reason.upper() in reasons
    assert "POST_BROKER_RECONCILIATION_FAILED" in reasons


def test_transient_post_broker_api_failure_defers_without_global_freeze(tmp_path, monkeypatch):
    count = 0

    def reconcile(_client, _store):
        nonlocal count
        count += 1
        if count == 2:
            raise MoomooUnavailable("transient market-state lookup failure")
        return {"ok": True}

    ex, _, store = executor(tmp_path, reconcile)
    monkeypatch.setattr(
        module, "dispatch_signed_preview",
        lambda *args, **kwargs: {"accepted": True, "order": {
            "order_id": "raw", "order_status": "SUBMITTED",
        }},
    )

    with pytest.raises(MoomooUnavailable):
        ex.execute_one(now=NOW)

    intent = store.list_auto_order_intents()[0]
    assert intent["status"] == "ACKED"
    assert store.snapshot().lifecycle == "ACTIVE"
    assert store.snapshot().freeze_reason is None


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
    assert store.snapshot().lifecycle == "ACTIVE"
    assert store.applicable_execution_holds(intent_id=intent["intent_id"])[0]["reason_code"] == "BROKER_ORDER_NOT_PROVEN"


def test_stale_reserved_config_is_cancelled_before_dispatch(tmp_path, monkeypatch):
    ex, _, store = executor(tmp_path)
    old = store.create_auto_order_intent(
        strategy_id="B16", config_version=1, signal_batch_id="b" * 64,
        signal_source_date="2026-08-25", factor_set_hash="f" * 64,
        symbol="US.DRAM", side="SELL", purpose="RANK_EXIT", target_qty=0,
        order_qty=1, limit_price=99,
    )
    store.update_config({"top_n": 5}, 1, "test", "new live config")
    store.resolve_execution_holds(
        scope_type="SYSTEM", scope_key="*",
        reason_code="CONTROL_GENERATION_CHANGED_REQUIRES_SYNC", source="control",
        resolved_by="test_reconciler", resolution_reason="fresh test sync",
    )
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
    assert any(row["config_version"] == 2 and row["status"] == "ACKED" for row in intents)


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
    assert store.snapshot().lifecycle == "ACTIVE"
    assert store.applicable_execution_holds(intent_id=intent["intent_id"])[0]["reason_code"] == "BROKER_ORDER_PROOF_MISMATCH"
