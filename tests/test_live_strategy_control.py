from __future__ import annotations

import sqlite3
import tarfile

import pytest

from core.live_strategy_control import (
    ControlRejected, EXPOSURE_CAP, INITIAL_CAPITAL, LOSS_FLOOR, LiveStrategyStore, utcnow,
)


def store(tmp_path):
    return LiveStrategyStore(tmp_path / "live.db", tmp_path / "archives")


def make_active(s: LiveStrategyStore):
    with s.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                    "freeze_reason=NULL,last_sync_at=? WHERE id=1", (utcnow(),))


def test_immutable_capital_boundaries_are_database_constraints(tmp_path):
    s = store(tmp_path)
    state = s.snapshot()
    assert state.initial_capital == INITIAL_CAPITAL == 10_000
    assert state.exposure_cap == EXPOSURE_CAP == 10_000
    assert state.loss_floor == LOSS_FLOOR == 7_500
    with pytest.raises(sqlite3.IntegrityError):
        with s.connect() as con:
            con.execute("UPDATE strategy_state SET exposure_cap=10001 WHERE id=1")


def test_system_starts_frozen_and_cannot_unfreeze_without_sync(tmp_path):
    s = store(tmp_path)
    assert s.snapshot().frozen
    with pytest.raises(ControlRejected, match="reconciliation"):
        s.unfreeze("test")


def test_unfreeze_requires_reconciliation_within_seven_minutes(tmp_path):
    s = store(tmp_path)
    with s.connect() as con:
        con.execute("UPDATE strategy_state SET last_sync_at='2000-01-01T00:00:00+00:00' WHERE id=1")
    with pytest.raises(ControlRejected, match="within 7 minutes"):
        s.unfreeze("stale")


def test_only_module_confirmed_fills_create_sellable_ownership(tmp_path):
    s = store(tmp_path)
    make_active(s)
    assert s.owned_quantity("US.AAPL") == 0
    with pytest.raises(ControlRejected, match="not acquired"):
        s.pretrade_guard("SELL", "US.AAPL", 1, 100)
    assert s.apply_fill("broker-fill-runtime-only", "US.AAPL", "BUY", 10, 100, 1)
    assert not s.apply_fill("broker-fill-runtime-only", "US.AAPL", "BUY", 10, 100, 1)
    assert s.owned_quantity("US.AAPL") == 10
    s.pretrade_guard("SELL", "US.AAPL", 10, 100)
    with pytest.raises(ControlRejected, match="not acquired"):
        s.pretrade_guard("SELL", "US.AAPL", 10.01, 100)


def test_buy_exposure_can_never_exceed_ten_thousand(tmp_path):
    s = store(tmp_path)
    make_active(s)
    s.apply_fill("f1", "US.AAPL", "BUY", 90, 100)
    s.mark_to_market({"US.AAPL": 100})
    s.pretrade_guard("BUY", "US.MSFT", 10, 100)
    with pytest.raises(ControlRejected, match="USD 10,000"):
        s.pretrade_guard("BUY", "US.MSFT", 10.01, 100)


def test_market_appreciation_over_cap_latches_freeze(tmp_path):
    s = store(tmp_path)
    make_active(s)
    s.apply_fill("f1", "US.AAPL", "BUY", 90, 100)
    state = s.mark_to_market({"US.AAPL": 112})
    assert state.frozen
    assert state.freeze_reason == "strategy_exposure_above_10000"


def test_equity_at_7500_latches_loss_floor_freeze(tmp_path):
    s = store(tmp_path)
    make_active(s)
    s.apply_fill("f1", "US.AAPL", "BUY", 100, 100)
    state = s.mark_to_market({"US.AAPL": 75})
    assert state.strategy_equity == 7500
    assert state.frozen
    assert state.freeze_reason == "strategy_equity_at_or_below_7500"
    with pytest.raises(ControlRejected, match="Loss-floor"):
        s.unfreeze("unsafe reset")


def test_hot_config_is_versioned_and_hard_limits_are_not_editable(tmp_path):
    s = store(tmp_path)
    make_active(s)
    current = s.config()
    updated = s.update_config({"stop_cooldown_hours": 48, "top_n": 5,
                               "position_target_pct": 0.17}, current["version"],
                              "tester", "shadow candidate")
    assert updated["version"] == current["version"] + 1
    assert updated["values"]["stop_cooldown_hours"] == 48
    assert s.snapshot().lifecycle == "FROZEN"
    assert s.snapshot().freeze_reason == "config_changed_requires_review"
    with pytest.raises(ControlRejected, match="post-freeze"):
        s.unfreeze("must not reuse pre-change sync")
    with pytest.raises(ControlRejected, match="reload"):
        s.update_config({"top_n": 4}, current["version"], "tester", "stale")
    with pytest.raises(ControlRejected, match="Unknown"):
        s.update_config({"exposure_cap": 20_000}, updated["version"], "tester", "forbidden")


def test_cleanup_requires_fresh_broker_zero_order_proof(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ControlRejected, match="broker proof"):
        s.cleanup("FREEZE ARCHIVE AND CLEAN STRATEGY", "tester", "poor performance", False)
    assert s.snapshot().lifecycle == "FROZEN"


def test_cleanup_requires_flat_then_archives_and_removes_valid_strategy(tmp_path):
    s = store(tmp_path)
    make_active(s)
    s.apply_fill("f1", "US.AAPL", "BUY", 1, 100)
    with pytest.raises(ControlRejected, match="zero strategy-owned"):
        s.cleanup("FREEZE ARCHIVE AND CLEAN STRATEGY", "tester", "poor performance", True)
    s.apply_fill("f2", "US.AAPL", "SELL", 1, 100)
    archive = s.cleanup("FREEZE ARCHIVE AND CLEAN STRATEGY", "tester", "poor performance", True)
    assert archive.exists()
    with tarfile.open(archive) as bundle:
        archived_db = tmp_path / "archived.db"
        member = bundle.extractfile("live_strategy.db")
        assert member is not None
        archived_db.write_bytes(member.read())
    with sqlite3.connect(archived_db) as con:
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert s.snapshot().lifecycle == "CLEANED"
    assert s.snapshot().strategy_id is None
    with pytest.raises(ControlRejected, match="No valid strategy"):
        s.unfreeze("try")


def test_events_redact_sensitive_runtime_fields(tmp_path):
    s = store(tmp_path)
    s.event("api", "test", "info", "request token=do-not-store account_id=12345678",
            {"account_id": "runtime", "token": "secret", "symbol": "US.AAPL"})
    event = s.recent_events(1)[0]
    assert event["details"]["account_id"] == "[REDACTED]"
    assert event["details"]["token"] == "[REDACTED]"
    assert event["details"]["symbol"] == "US.AAPL"
    assert "do-not-store" not in event["message"]
    assert "12345678" not in event["message"]
