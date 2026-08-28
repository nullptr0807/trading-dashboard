from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.live_strategy_control import LiveStrategyStore


_spec = importlib.util.spec_from_file_location(
    "live_eod_audit_packet_under_test",
    Path(__file__).resolve().parents[1] / "scripts" / "live_eod_audit_packet.py",
)
assert _spec and _spec.loader
eod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eod)


def test_strategy_evidence_contains_complete_reconstructable_fee_ledgers(tmp_path, monkeypatch):
    db = tmp_path / "strategy.db"
    store = LiveStrategyStore(db, tmp_path / "archives")
    order_hash = "a" * 64
    with store.connect() as con:
        con.execute(
            "INSERT INTO applied_fills VALUES(?,?,?,?,?,?,?,?,?)",
            ("b" * 64, "US.AAPL", "BUY", 2, 100, 0.6,
             "2026-08-28T15:00:00+00:00", 1, order_hash),
        )
        con.execute(
            "INSERT INTO order_fee_accounts VALUES(?,?,?,?,?,?,?)",
            (order_hash, "US.AAPL", "BUY", 1.0, 1, 2,
             "2026-08-28T16:00:00+00:00"),
        )
        con.execute(
            "INSERT INTO order_fee_adjustments "
            "(adjustment_hash,order_hash,previous_total,new_total,fill_fee_credit,delta,applied_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("c" * 64, order_hash, 0.0, 1.0, 0.0, 1.0,
             "2026-08-28T15:00:00+00:00"),
        )
        con.execute(
            "INSERT INTO order_fee_adjustments "
            "(adjustment_hash,order_hash,previous_total,new_total,fill_fee_credit,delta,applied_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("d" * 64, order_hash, 1.0, 1.0, 0.6, -0.6,
             "2026-08-28T16:00:00+00:00"),
        )
    monkeypatch.setattr(eod, "STRATEGY_DB", db)
    start = datetime(2026, 8, 28, tzinfo=timezone.utc)

    evidence = eod.collect_strategy(start, start + timedelta(days=1))

    assert evidence["fee_accounting"]["fill_fee_total"] == pytest.approx(0.6)
    assert evidence["fee_accounting"]["adjustment_delta_total"] == pytest.approx(0.4)
    assert evidence["fee_accounting"]["reconstructed_total_fees"] == pytest.approx(1.0)
    assert evidence["order_fee_accounts_at_collection"][0]["revision"] == 2
    assert evidence["order_fee_accounts_at_collection"][0]["cumulative_fee"] == 1.0
    assert evidence["order_fee_adjustments_at_collection"][1]["fill_fee_credit"] == 0.6
    assert evidence["fee_accounting"]["by_order"][0]["matches_current_total"] is True
    assert evidence["fee_accounting"]["by_order"][0]["revision"] == 2
    assert len(evidence["applied_fills_at_collection"]) == 1
    serialized = json.dumps(evidence)
    assert order_hash not in serialized
    assert "sha256:" in serialized


def test_eod_application_log_sanitization_cannot_reemit_generic_failure_secrets():
    malicious = (
        "token abcNaturalToken order_id ORDER-ABC-998877 "
        "deal reference DEAL-SECRET-776655 account 123456789 "
        "broker reference BRK-REFERENCE-445566 "
        "Authorization: Bearer bearer-secret-0123456789 "
        "opaque_ZYXWVUTSRQPONMLK987654321 "
        "order reference ORD.NATURAL-1 broker order: BRK=NATURAL-2 "
        "deal ref=D.NATURAL-3 account number ACCT.NATURAL-4"
    )

    serialized = json.dumps(eod.sanitize({"message": malicious, "data": {"error": malicious}}))

    for secret in (
        "abcNaturalToken", "ORDER-ABC-998877", "DEAL-SECRET-776655",
        "123456789", "BRK-REFERENCE-445566", "bearer-secret-0123456789",
        "opaque_ZYXWVUTSRQPONMLK987654321", "ORD.NATURAL-1",
        "BRK=NATURAL-2", "D.NATURAL-3", "ACCT.NATURAL-4",
    ):
        assert secret not in serialized