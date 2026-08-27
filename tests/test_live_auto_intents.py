from __future__ import annotations

import json
import hashlib
import sqlite3

import pytest

from core.live_strategy_control import ControlRejected, LiveStrategyStore, utcnow


def store(tmp_path) -> LiveStrategyStore:
    s = LiveStrategyStore(tmp_path / "live.db", tmp_path / "archives")
    with s.connect() as con:
        con.execute(
            "UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
            "freeze_reason=NULL,last_sync_at=? WHERE id=1",
            (utcnow(),),
        )
    return s


def intent_kwargs(**overrides):
    values = {
        "strategy_id": "B16",
        "config_version": 1,
        "signal_batch_id": "b" * 64,
        "signal_source_date": "2026-08-26",
        "factor_set_hash": "a" * 64,
        "purpose": "TARGET_BUY",
        "symbol": "US.AAPL",
        "side": "BUY",
        "target_qty": 10,
        "order_qty": 2,
        "limit_price": 100,
    }
    values.update(overrides)
    return values


def test_duplicate_cron_returns_same_durable_reserved_intent(tmp_path):
    s = store(tmp_path)
    first = s.create_auto_order_intent(**intent_kwargs())
    second = s.create_auto_order_intent(**intent_kwargs())

    assert first == second
    assert first["status"] == "RESERVED"
    assert first["reserved_notional"] == pytest.approx(200)
    assert first["reserved_sell_qty"] == 0
    assert len(s.list_auto_order_intents()) == 1
    assert s.get_auto_order_intent(first["intent_id"]) == first


def test_intent_hashes_are_deterministic_across_databases(tmp_path):
    first = store(tmp_path / "one").create_auto_order_intent(**intent_kwargs())
    second = store(tmp_path / "two").create_auto_order_intent(**intent_kwargs())
    assert first["intent_id"] == second["intent_id"]
    assert first["payload_hash"] == second["payload_hash"]


def test_creation_requires_active_matching_config_and_configured_limits(tmp_path):
    s = store(tmp_path)
    with s.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1 WHERE id=1")
    with pytest.raises(ControlRejected, match="FROZEN"):
        s.create_auto_order_intent(**intent_kwargs())

    with s.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0 WHERE id=1")
    with pytest.raises(ControlRejected, match="active strategy configuration"):
        s.create_auto_order_intent(**intent_kwargs(config_version=2))
    with pytest.raises(ControlRejected, match="maximum order"):
        s.create_auto_order_intent(**intent_kwargs(order_qty=26, limit_price=100))
    with pytest.raises(ControlRejected, match="daily order"):
        s.create_auto_order_intent(**intent_kwargs(), daily_order_notional=4_900)
    with pytest.raises(ControlRejected, match="B16 US"):
        s.create_auto_order_intent(**intent_kwargs(symbol="AAPL"))
    with pytest.raises(ControlRejected, match="SHA-256"):
        s.create_auto_order_intent(**intent_kwargs(signal_batch_id="not-a-hash"))
    with pytest.raises(ControlRejected, match="purpose"):
        s.create_auto_order_intent(**intent_kwargs(purpose="RANK_EXIT"))
    with pytest.raises(ControlRejected, match="whole-share"):
        s.create_auto_order_intent(**intent_kwargs(order_qty=1.5))


def test_deterministic_key_conflicting_payload_freezes_and_rejects(tmp_path):
    s = store(tmp_path)
    original = s.create_auto_order_intent(**intent_kwargs(order_qty=2, limit_price=100))

    with pytest.raises(ControlRejected, match="payload conflict"):
        s.create_auto_order_intent(**intent_kwargs(order_qty=2, limit_price=101))

    assert s.snapshot().lifecycle == "FROZEN"
    assert s.snapshot().freeze_reason == "auto_intent_payload_conflict"
    assert s.get_auto_order_intent(original["intent_id"])["status"] == "RESERVED"


def test_partial_cancel_remainder_uses_new_at_most_once_slice(tmp_path):
    s = store(tmp_path)
    first = s.create_auto_order_intent(**intent_kwargs(order_qty=10, target_qty=10))
    s.mark_auto_intent_dispatching(first["intent_id"], "partial-preview")
    s.mark_auto_intent_partial(first["intent_id"])
    s.mark_auto_intent_cancelled(first["intent_id"], "PARTIAL_CANCEL")
    remainder = s.create_auto_order_intent(**intent_kwargs(order_qty=4, target_qty=10))
    assert remainder["intent_id"] != first["intent_id"]
    assert remainder["status"] == "RESERVED"


def test_buy_reservation_counts_broker_pending_and_other_auto_intents(tmp_path):
    s = store(tmp_path)
    first = s.create_auto_order_intent(**intent_kwargs(symbol="US.AAPL", order_qty=20, limit_price=100))
    s.mark_auto_intent_cancelled(first["intent_id"])
    # A terminal reservation is released.
    second = s.create_auto_order_intent(
        **intent_kwargs(symbol="US.MSFT", order_qty=20, limit_price=100),
        broker_pending_buy_notional=7_900,
    )
    assert second["reserved_notional"] == pytest.approx(2_000)

    # A different order cannot be created while any reserved intent is active.
    with pytest.raises(ControlRejected, match="unresolved"):
        s.create_auto_order_intent(
            **intent_kwargs(symbol="US.NVDA", order_qty=2, limit_price=100),
            broker_pending_buy_notional=7_900,
        )

    assert s.auto_intent_reservations() == {
        "reserved_buy_notional": pytest.approx(2_000),
        "reserved_sell_qty": {},
    }
    assert s.auto_intent_reservations(exclude_intent_id=second["intent_id"])[
        "reserved_buy_notional"
    ] == 0

    s.mark_auto_intent_cancelled(second["intent_id"])
    with s.connect() as con:
        con.execute("UPDATE strategy_state SET reserved_buy_notional=9900 WHERE id=1")
    with pytest.raises(ControlRejected, match="exposure"):
        s.create_auto_order_intent(**intent_kwargs(symbol="US.GOOG"))


def test_sell_reservations_cannot_exceed_strategy_owned_or_broker_pending(tmp_path):
    s = store(tmp_path)
    s.apply_fill("verified-fill", "US.AAPL", "BUY", 10, 10)

    first = s.create_auto_order_intent(
        **intent_kwargs(side="SELL", purpose="RANK_EXIT", order_qty=4, limit_price=11)
    )
    assert first["reserved_sell_qty"] == 4
    assert first["reserved_notional"] == 0

    with pytest.raises(ControlRejected, match="unresolved"):
        s.create_auto_order_intent(
            **intent_kwargs(
                side="SELL", purpose="STOP_LOSS", target_qty=0,
                order_qty=2, limit_price=9,
            ),
            broker_pending_sell_qty=5,
        )
    assert s.auto_intent_reservations()["reserved_sell_qty"] == {"US.AAPL": 4.0}
    s.mark_auto_intent_cancelled(first["intent_id"])
    with pytest.raises(ControlRejected, match="strategy-owned"):
        s.create_auto_order_intent(
            **intent_kwargs(
                side="SELL", purpose="STOP_LOSS", target_qty=0,
                order_qty=6, limit_price=9,
            ),
            broker_pending_sell_qty=5,
        )


def test_unresolved_dispatch_blocks_new_intents_and_unknown_never_retries(tmp_path):
    s = store(tmp_path)
    intent = s.create_auto_order_intent(**intent_kwargs())
    dispatching = s.mark_auto_intent_dispatching(intent["intent_id"], "preview-safe-1")
    assert dispatching["status"] == "DISPATCHING"
    assert dispatching["preview_id"] == "preview-safe-1"

    with pytest.raises(ControlRejected, match="unresolved"):
        s.create_auto_order_intent(**intent_kwargs(symbol="US.MSFT"))
    with pytest.raises(ControlRejected, match="transition"):
        s.mark_auto_intent_dispatching(intent["intent_id"], "preview-safe-1")

    unknown = s.mark_auto_intent_unknown(intent["intent_id"], "BROKER_OUTCOME_UNKNOWN")
    assert unknown["status"] == "UNKNOWN"
    assert unknown["error_code"] == "BROKER_OUTCOME_UNKNOWN"
    with pytest.raises(ControlRejected, match="transition"):
        s.mark_auto_intent_dispatching(intent["intent_id"], "preview-safe-1")
    with pytest.raises(ControlRejected, match="unresolved"):
        s.create_auto_order_intent(**intent_kwargs(symbol="US.NVDA"))

    # Reconciliation may prove a terminal outcome, after which creation resumes.
    assert s.mark_auto_intent_cancelled(intent["intent_id"])["status"] == "CANCELLED"
    assert s.create_auto_order_intent(**intent_kwargs(symbol="US.NVDA"))["status"] == "RESERVED"


def test_ack_partial_fill_and_failure_transitions_are_legal_only(tmp_path):
    s = store(tmp_path)
    intent = s.create_auto_order_intent(**intent_kwargs())
    s.mark_auto_intent_dispatching(intent["intent_id"], "preview-safe-2")
    assert s.mark_auto_intent_acked(intent["intent_id"])["status"] == "ACKED"
    assert s.auto_intent_reservations()["reserved_buy_notional"] == pytest.approx(200)
    with pytest.raises(ControlRejected, match="unresolved"):
        s.create_auto_order_intent(**intent_kwargs(symbol="US.BLOCKED"))
    s.handoff_auto_intent_reservation(intent["intent_id"])
    assert s.auto_intent_reservations()["reserved_buy_notional"] == 0
    assert s.mark_auto_intent_partial(intent["intent_id"])["status"] == "PARTIAL"
    assert s.mark_auto_intent_filled(intent["intent_id"])["status"] == "FILLED"
    with pytest.raises(ControlRejected, match="transition"):
        s.mark_auto_intent_failed(intent["intent_id"], "TOO_LATE")

    failed = s.create_auto_order_intent(**intent_kwargs(symbol="US.MSFT"))
    s.mark_auto_intent_dispatching(failed["intent_id"], "preview-safe-3")
    assert s.mark_auto_intent_failed(failed["intent_id"], "BROKER_REJECTED")["status"] == "FAILED"


def test_schema_and_rows_never_store_raw_broker_references_or_secrets(tmp_path):
    s = store(tmp_path)
    intent = s.create_auto_order_intent(**intent_kwargs())
    s.mark_auto_intent_dispatching(intent["intent_id"], "preview-safe-4")
    with pytest.raises(ControlRejected, match="error_code"):
        s.mark_auto_intent_unknown(intent["intent_id"], "token=secret order_id=12345678")

    with sqlite3.connect(s.path) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(auto_order_intents)")}
        row = con.execute("SELECT * FROM auto_order_intents").fetchone()
        serialized = json.dumps(row).lower()

    assert not ({"order_id", "deal_id", "account_id", "token", "secret"} & columns)
    assert "12345678" not in serialized
    assert "token=secret" not in serialized
    assert {
        "intent_id", "payload_hash", "strategy_id", "config_version", "signal_batch_id",
        "signal_source_date", "factor_set_hash", "symbol", "side", "purpose", "target_qty",
        "order_qty", "limit_price", "status", "preview_id", "reserved_notional",
        "reserved_sell_qty", "created_at", "updated_at", "error_code",
    } <= columns


def test_read_only_store_never_creates_or_mutates_database(tmp_path):
    missing = tmp_path / "missing.db"
    readonly_missing = LiveStrategyStore(missing, tmp_path / "archives", read_only=True)
    assert not missing.exists()
    with pytest.raises(FileNotFoundError):
        readonly_missing.snapshot()
    assert not missing.exists()

    writable = store(tmp_path / "existing")
    before_hash = hashlib.sha256(writable.path.read_bytes()).hexdigest()
    before_mtime = writable.path.stat().st_mtime_ns
    readonly = LiveStrategyStore(writable.path, tmp_path / "archives", read_only=True)
    assert readonly.snapshot().strategy_id == "B16"
    assert readonly.list_auto_order_intents() == []
    assert hashlib.sha256(writable.path.read_bytes()).hexdigest() == before_hash
    assert writable.path.stat().st_mtime_ns == before_mtime
    with pytest.raises(sqlite3.OperationalError):
        readonly.event("forbidden", "test", "info", "must fail", {})
