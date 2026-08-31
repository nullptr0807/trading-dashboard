from __future__ import annotations

import io
import json
import logging
import sqlite3
import subprocess
import sys
import tarfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.live_strategy_control as live_control
from core.live_logging import JsonFormatter, redact
from core.live_strategy_control import (
    ControlRejected, EXPOSURE_CAP, INITIAL_CAPITAL, LOSS_FLOOR, LiveStrategyStore, utcnow,
)


def store(tmp_path):
    return LiveStrategyStore(tmp_path / "live.db", tmp_path / "archives")


def make_active(s: LiveStrategyStore):
    with s.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                    "freeze_reason=NULL,last_sync_at=? WHERE id=1", (utcnow(),))


def assert_active_hold(s: LiveStrategyStore, reason: str, *, scope="SYSTEM", key="*"):
    holds = s.list_execution_holds(active_only=True)
    assert any(h["reason_code"] == reason and h["scope_type"] == scope
               and h["scope_key"] == key for h in holds), holds


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


def test_direct_store_unfreeze_requires_matching_generation_proof(tmp_path):
    s = store(tmp_path)
    state = s.mark_to_market({}, sync_complete=True)
    s.observe_runtime_fingerprint("old-generation")
    s.record_broker_sync_proof("old-generation", str(state.last_sync_at))
    s.observe_runtime_fingerprint("current-generation")
    state = s.mark_to_market({}, sync_complete=True)
    with pytest.raises(ControlRejected, match="matching broker sync proof"):
        s.unfreeze("stale generation")
    s.record_broker_sync_proof("current-generation", str(state.last_sync_at))
    active = s.unfreeze("verified generation")
    assert active.lifecycle == "ACTIVE"


def test_config_change_immediately_rotates_generation_and_deletes_proof(tmp_path):
    s = store(tmp_path)
    fingerprint = "runtime-v1"
    generation = s.observe_runtime_fingerprint(fingerprint)
    state = s.mark_to_market({}, sync_complete=True)
    s.record_broker_sync_proof(fingerprint, str(state.last_sync_at))
    assert s.broker_sync_proof_matches(fingerprint)
    current = s.config()
    s.update_config({"stop_cooldown_hours": 48}, current["version"], "test", "generation test")
    assert s.current_control_generation() == generation + 1
    assert not s.broker_sync_proof_matches(fingerprint)
    assert s.snapshot().lifecycle == "FROZEN"  # original operator state is untouched
    assert_active_hold(s, "CONTROL_GENERATION_CHANGED_REQUIRES_SYNC")


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


def test_strategy_execution_summary_and_fill_history(tmp_path):
    s = store(tmp_path)
    assert s.apply_fill("buy-1", "US.DRAM", "BUY", 2, 10, 1.0)
    assert s.apply_fill("sell-1", "US.DRAM", "SELL", 1, 12, 0.5)
    summary = s.execution_summary()
    assert summary["total_trades"] == 2
    assert summary["buy_trades"] == 1
    assert summary["sell_trades"] == 1
    assert summary["total_fees"] == pytest.approx(1.5)
    assert summary["total_notional"] == pytest.approx(32)
    fills = s.fills()
    assert len(fills) == 2
    assert set(fills[0]) == {"symbol", "side", "quantity", "price", "fee", "applied_at"}
    display = s.fill_display_history()
    assert display[0]["effective_fee"] == pytest.approx(0.5)
    assert display[0]["fee_finalized"] == 1
    assert all("fill_hash" not in row for row in fills)


def test_symbol_performance_ranks_closed_and_open_positions_by_total_pnl(tmp_path):
    s = store(tmp_path)
    s.apply_fill("aaa-buy", "US.AAA", "BUY", 1, 10, 1.0)
    s.apply_fill("aaa-sell", "US.AAA", "SELL", 1, 12, 0.5)
    s.apply_fill("bbb-buy", "US.BBB", "BUY", 2, 10, 1.0)
    s.mark_to_market({"US.BBB": 11})

    # A cumulative order-fee account supersedes its fill fee; it must not be
    # counted a second time in per-symbol performance.
    order_hash = "b" * 64
    with s.connect() as con:
        con.execute(
            "UPDATE applied_fills SET order_hash=? WHERE symbol='US.BBB'",
            (order_hash,),
        )
        con.execute(
            "INSERT INTO order_fee_accounts"
            "(order_hash,symbol,side,cumulative_fee,finalized,revision,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (order_hash, "US.BBB", "BUY", 1.0, 1, 1, utcnow()),
        )

    rows = s.symbol_performance()
    assert [row["symbol"] for row in rows] == ["US.BBB", "US.AAA"]

    held, closed = rows
    assert held["holding"] is True
    assert held["quantity"] == 2
    assert held["fees"] == pytest.approx(1.0)
    assert held["unrealized_pnl"] == pytest.approx(1.0)
    assert held["realized_pnl"] == pytest.approx(0.0)
    assert held["total_pnl"] == pytest.approx(1.0)
    assert held["return_pct"] == pytest.approx(100 / 21)

    assert closed["holding"] is False
    assert closed["quantity"] == 0
    assert closed["unrealized_pnl"] == 0
    assert closed["realized_pnl"] == pytest.approx(0.5)
    assert closed["total_pnl"] == pytest.approx(0.5)
    assert sum(row["total_pnl"] for row in rows) == pytest.approx(
        s.snapshot().strategy_equity - INITIAL_CAPITAL
    )


def test_performance_summary_requires_enough_daily_sharpe_observations(tmp_path):
    s = store(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with s.connect() as con:
        for day in range(22):
            equity = 10_000 + day * 10 + (5 if day % 2 else -5)
            con.execute(
                "INSERT INTO strategy_equity(ts,equity,cash,market_value,realized_pnl,unrealized_pnl,lifecycle) "
                "VALUES(?,?,?,?,?,?,?)",
                ((start + timedelta(days=day)).isoformat(), equity, equity, 0, 0, 0, "ACTIVE"),
            )
        con.execute("UPDATE strategy_state SET strategy_equity=? WHERE id=1", (10_205,))
    summary = s.performance_summary()
    assert summary["pnl"] == pytest.approx(205)
    assert summary["total_return_pct"] == pytest.approx(2.05)
    assert summary["sharpe_observations"] == 21
    assert summary["sharpe_ratio"] is not None
    assert summary["max_drawdown_pct"] <= 0


def test_buy_exposure_can_never_exceed_ten_thousand(tmp_path):
    s = store(tmp_path)
    make_active(s)
    s.apply_fill("f1", "US.AAPL", "BUY", 90, 100)
    s.mark_to_market({"US.AAPL": 100})
    s.pretrade_guard("BUY", "US.MSFT", 10, 100)
    with pytest.raises(ControlRejected, match="USD 10,000"):
        s.pretrade_guard("BUY", "US.MSFT", 10.01, 100)


def test_market_appreciation_over_cap_latches_system_hold(tmp_path):
    s = store(tmp_path)
    make_active(s)
    s.apply_fill("f1", "US.AAPL", "BUY", 90, 100)
    state = s.mark_to_market({"US.AAPL": 112})
    assert state.lifecycle == "ACTIVE"
    assert_active_hold(s, "STRATEGY_EXPOSURE_ABOVE_10000")


def test_equity_at_7500_latches_loss_floor_hold(tmp_path):
    s = store(tmp_path)
    make_active(s)
    s.apply_fill("f1", "US.AAPL", "BUY", 100, 100)
    state = s.mark_to_market({"US.AAPL": 75})
    assert state.strategy_equity == 7500
    assert state.lifecycle == "ACTIVE"
    assert_active_hold(s, "STRATEGY_EQUITY_AT_OR_BELOW_7500")
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
    assert s.snapshot().lifecycle == "ACTIVE"
    assert_active_hold(s, "CONTROL_GENERATION_CHANGED_REQUIRES_SYNC")
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


def test_events_redact_natural_language_broker_references_and_long_identifiers(tmp_path):
    s = store(tmp_path)
    secrets = [
        "abcNaturalToken", "ORDER-ABC-998877", "DEAL-SECRET-776655",
        "123456789", "BRK-REFERENCE-445566", "bearer-secret-0123456789",
        "opaque_ZYXWVUTSRQPONMLK987654321", "ORD.NATURAL-1",
        "BRK=NATURAL-2", "D.NATURAL-3", "ACCT.NATURAL-4",
    ]
    message = (
        "token abcNaturalToken order_id ORDER-ABC-998877 "
        "deal reference DEAL-SECRET-776655 account 123456789 "
        "broker reference BRK-REFERENCE-445566 "
        "Authorization Bearer bearer-secret-0123456789 "
        "opaque_ZYXWVUTSRQPONMLK987654321 "
        "order reference ORD.NATURAL-1 broker order: BRK=NATURAL-2 "
        "deal ref=D.NATURAL-3 account number ACCT.NATURAL-4"
    )

    s.event("api", "test", "critical", message, {"error": message})

    serialized = json.dumps(s.recent_events(1), sort_keys=True)
    assert all(secret not in serialized for secret in secrets)


def test_redaction_covers_plain_identifier_labels_and_complete_authorization_values():
    malicious = (
        "order ORD12 deal DEAL34 broker BRK56 account ACCT78; "
        "Authorization: Basic NATURALSECRETONLYLETTERS; "
        "authorization=Digest digest-user-secret, "
        "AUTHORIZATION CustomScheme dotted.secret-value\nnext line"
    )
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.Logger("redaction-regression")
    logger.addHandler(handler)
    logger.info(malicious, extra={"structured": {"error": malicious}})

    outputs = [str(redact(malicious)), stream.getvalue()]
    for output in outputs:
        for secret in (
            "ORD12", "DEAL34", "BRK56", "ACCT78",
            "NATURALSECRETONLYLETTERS", "digest-user-secret", "dotted.secret-value",
        ):
            assert secret not in output
        assert "next line" in output
    assert redact(redact(malicious)) == redact(malicious)
    ordinary = "error code RECONCILIATION_INTERNAL_ERROR order failed account unavailable deal rejected broker offline"
    assert redact(ordinary) == ordinary


def test_redaction_closes_authorization_json_key_and_plain_word_bypasses(tmp_path):
    payload = {
        "Authorization": "Digest realm=\"private\", nonce=\"NonceSecret\", uri=\"/private\"",
        "order alphaidentifier": "key-secret",
        "nested": [
            {"deal": "DealToken", "safe": "order failed"},
            {"account": "AccountToken", "broker": "BrokerToken"},
        ],
    }
    quoted = (
        '{"Authorization":"AWS4-HMAC-SHA256 Credential=AccessSecret/region, '
        'SignedHeaders=host;x-private, Signature=SignatureSecret","safe":"visible"}'
    )
    cleaned = redact(payload)
    serialized = json.dumps(cleaned, sort_keys=True)
    quoted_cleaned = str(redact(quoted))
    for marker in (
        "private", "NonceSecret", "/private", "alphaidentifier", "key-secret",
        "DealToken", "AccountToken", "BrokerToken", "AccessSecret", "x-private",
        "SignatureSecret",
    ):
        assert marker not in serialized + quoted_cleaned
    assert cleaned["nested"][0]["safe"] == "order failed"
    assert '"safe":"visible"' in quoted_cleaned

    s = store(tmp_path)
    s.event("api", "test", "critical", quoted, payload)
    persisted = json.dumps(s.recent_events(1), sort_keys=True)
    assert all(marker not in persisted for marker in (
        "NonceSecret", "alphaidentifier", "DealToken", "AccessSecret", "SignatureSecret",
    ))


def test_redaction_bounds_and_escapes_untrusted_multiline_text():
    result = str(redact("first\r\nAuthorization Bearer secret\n" + "x" * 20_000))
    assert "\n" not in result and "\r" not in result
    assert "secret" not in result
    assert len(result) <= 4_200


def test_freeze_never_persists_caller_controlled_reason_text(tmp_path):
    s = store(tmp_path)
    malicious = "operator order ORD12 Authorization: Basic NATURALSECRETONLYLETTERS"

    state = s.freeze(malicious, "dashboard")

    assert state.freeze_reason == "operator_requested_freeze"
    with s.connect() as con:
        persisted = con.execute("SELECT freeze_reason FROM strategy_state WHERE id=1").fetchone()[0]
        rows = con.execute("SELECT message,details_json FROM strategy_events").fetchall()
    assert persisted == "operator_requested_freeze"
    serialized = json.dumps([tuple(row) for row in rows])
    assert "ORD12" not in serialized
    assert "NATURALSECRETONLYLETTERS" not in serialized
    assert any(event["event_type"] == "freeze_reason_sanitized" for event in s.recent_events(10))


def test_existing_unsafe_freeze_reason_is_sanitized_when_store_reopens(tmp_path):
    db = tmp_path / "legacy.db"
    s = LiveStrategyStore(db, tmp_path / "archives")
    with s.connect() as con:
        con.execute(
            "UPDATE strategy_state SET freeze_reason=? WHERE id=1",
            ("order ORD12 Authorization Bearer NATURALSECRET",),
        )

    reopened = LiveStrategyStore(db, tmp_path / "archives")

    assert reopened.snapshot().freeze_reason == "sanitized_freeze_reason"
    with reopened.connect() as con:
        assert con.execute("SELECT freeze_reason FROM strategy_state WHERE id=1").fetchone()[0] == (
            "sanitized_freeze_reason"
        )


def test_automation_cannot_write_manual_lifecycle_and_operator_can(tmp_path):
    s = store(tmp_path)
    make_active(s)
    for actor in ("internal", "auto_executor", "health_watchdog", "moomoo_reconciler"):
        with pytest.raises(ControlRejected, match="operator-only"):
            s.freeze("manual_freeze", actor)
        with pytest.raises(ControlRejected, match="operator-only"):
            s.unfreeze("automation may not release", actor)
    state = s.freeze("private operator note", "dashboard")
    assert state.lifecycle == "FROZEN"
    assert state.freeze_reason == "operator_requested_freeze"
    serialized = json.dumps(s.recent_events(50), sort_keys=True)
    assert "private operator note" not in serialized


def test_concurrent_first_open_serializes_schema_migration(tmp_path):
    path = tmp_path / "concurrent.db"
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE applied_fills (
            fill_hash TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
            quantity REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
            applied_at TEXT NOT NULL
        )""")
    barrier = threading.Barrier(12)
    failures = []

    def open_store():
        try:
            barrier.wait()
            LiveStrategyStore(path, tmp_path / "archives").snapshot()
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in con.execute("PRAGMA table_info(applied_fills)")}
        assert {"fee_is_stable", "order_hash"} <= columns
        state_columns = {row[1] for row in con.execute("PRAGMA table_info(strategy_state)")}
        assert "required_sync_after" in state_columns
        proof_columns = {row[1] for row in con.execute("PRAGMA table_info(broker_sync_proof)")}
        assert "control_generation" in proof_columns
        indexes = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        assert {"one_active_config", "auto_order_intents_status"} <= indexes
        assert con.execute("SELECT COUNT(*) FROM strategy_state").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM strategy_config WHERE active=1").fetchone()[0] == 1

    # Reopening an already migrated database is idempotent.
    LiveStrategyStore(path, tmp_path / "archives").snapshot()
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM strategy_state").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM strategy_config WHERE active=1").fetchone()[0] == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_concurrent_process_first_open_is_retry_safe(tmp_path, legacy):
    path = tmp_path / "processes.db"
    if legacy:
        with sqlite3.connect(path) as con:
            con.execute("""CREATE TABLE applied_fills (
                fill_hash TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
                quantity REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
                applied_at TEXT NOT NULL
            )""")
    code = (
        "from core.live_strategy_control import LiveStrategyStore;"
        f"LiveStrategyStore({str(path)!r}, {str(tmp_path / 'archives')!r}).snapshot()"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", code], cwd=str(Path(__file__).parents[1]))
        for _ in range(8)
    ]
    return_codes = [process.wait(timeout=30) for process in processes]

    assert return_codes == [0] * 8
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in con.execute("PRAGMA table_info(applied_fills)")}
        assert {"fee_is_stable", "order_hash"} <= columns
        assert con.execute("SELECT COUNT(*) FROM strategy_state").fetchone()[0] == 1


def test_wal_initialization_fails_closed_after_bounded_lock_timeout(tmp_path, monkeypatch):
    class LockedConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(live_control, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(live_control.sqlite3, "connect", lambda *_args, **_kwargs: LockedConnection())

    with pytest.raises(sqlite3.OperationalError, match="Timed out enabling WAL journal mode"):
        LiveStrategyStore(tmp_path / "locked.db", tmp_path / "archives")


def test_execution_hold_scope_and_exact_recovery(tmp_path):
    s = store(tmp_path)
    make_active(s)
    system = s.create_execution_hold("SYSTEM", "ignored", "SYNC_STALE", "test_system")
    symbol = s.create_execution_hold("SYMBOL", "us.aapl", "QUOTE_STALE", "test_quote")
    intent = s.create_execution_hold("INTENT", "intent-1", "OUTCOME_UNKNOWN", "test_executor")

    assert system["scope_key"] == "*"
    assert {h["reason_code"] for h in s.applicable_execution_holds(symbol="US.MSFT")} == {"SYNC_STALE"}
    assert {h["reason_code"] for h in s.applicable_execution_holds(symbol="US.AAPL")} == {
        "SYNC_STALE", "QUOTE_STALE",
    }
    assert {h["reason_code"] for h in s.applicable_execution_holds(intent_id="intent-1")} == {
        "SYNC_STALE", "OUTCOME_UNKNOWN",
    }
    assert s.resolve_execution_holds(
        scope_type="SYMBOL", scope_key="US.AAPL", reason_code="QUOTE_STALE",
        source="test_quote", resolved_by="test_recovery", resolution_reason="fresh quote",
    ) == 1
    assert s.resolve_execution_holds(
        scope_type="SYMBOL", scope_key="US.AAPL", reason_code="QUOTE_STALE",
        source="test_quote", resolved_by="test_recovery", resolution_reason="fresh quote",
    ) == 0
    assert {h["reason_code"] for h in s.list_execution_holds(active_only=True)} == {
        "SYNC_STALE", "OUTCOME_UNKNOWN",
    }


def test_concurrent_legacy_automatic_freeze_migration_is_idempotent(tmp_path):
    db = tmp_path / "legacy-auto-concurrent.db"
    original = LiveStrategyStore(db, tmp_path / "archives")
    with original.connect() as con:
        con.execute(
            "UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
            "freeze_reason='five_minute_reconciliation_failed' WHERE id=1"
        )
    barrier = threading.Barrier(8)
    failures = []

    def reopen():
        try:
            barrier.wait()
            LiveStrategyStore(db, tmp_path / "archives").snapshot()
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=reopen) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    migrated = LiveStrategyStore(db, tmp_path / "archives")
    assert migrated.snapshot().lifecycle == "ACTIVE"
    holds = migrated.list_execution_holds(active_only=True)
    assert [(row["reason_code"], row["source"]) for row in holds] == [
        ("FIVE_MINUTE_RECONCILIATION_FAILED", "moomoo_sync")
    ]
    assert sum(
        row["event_type"] == "legacy_automatic_freeze_migrated"
        for row in migrated.recent_events(100)
    ) == 1


def test_legacy_automatic_freeze_migrates_but_manual_freeze_is_preserved(tmp_path):
    auto_db = tmp_path / "legacy-auto.db"
    auto = LiveStrategyStore(auto_db, tmp_path / "auto-archives")
    with auto.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
                    "freeze_reason='five_minute_reconciliation_failed' WHERE id=1")
    migrated = LiveStrategyStore(auto_db, tmp_path / "auto-archives")
    assert migrated.snapshot().lifecycle == "ACTIVE"
    assert_active_hold(migrated, "FIVE_MINUTE_RECONCILIATION_FAILED")
    assert migrated.list_execution_holds(active_only=True)[0]["source"] == "moomoo_sync"

    manual_db = tmp_path / "legacy-manual.db"
    manual = LiveStrategyStore(manual_db, tmp_path / "manual-archives")
    with manual.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
                    "freeze_reason='manual_freeze' WHERE id=1")
    preserved = LiveStrategyStore(manual_db, tmp_path / "manual-archives")
    assert preserved.snapshot().lifecycle == "FROZEN"
    assert preserved.snapshot().freeze_reason == "manual_freeze"
    assert preserved.list_execution_holds(active_only=True) == []
