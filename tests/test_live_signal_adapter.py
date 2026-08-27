from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from core.live_signal_adapter import SignalAdapterError, load_b16_signal_batch


def _fixture(tmp_path, *, factors=("f1", "f2"), latest_rows=None, prior_rows=None):
    factors_path = tmp_path / "factors.json"
    factors_path.write_text(json.dumps({
        "B16": [{"name": name, "expression": f"expr_{name}"} for name in factors]
    }))
    db_path = tmp_path / "factors.db"
    with sqlite3.connect(db_path) as con:
        con.execute("""CREATE TABLE factor_values(
            ticker TEXT NOT NULL, date TEXT NOT NULL, factor_name TEXT NOT NULL,
            value REAL, factor_group TEXT NOT NULL,
            PRIMARY KEY(ticker,date,factor_name,factor_group))""")
        if prior_rows:
            con.executemany("INSERT INTO factor_values VALUES(?,?,?,?,?)", prior_rows)
        if latest_rows:
            con.executemany("INSERT INTO factor_values VALUES(?,?,?,?,?)", latest_rows)
    return db_path, factors_path


def _rows(date, values):
    return [(ticker, date, factor, value, "gp_B16")
            for ticker, by_factor in values.items()
            for factor, value in by_factor.items()]


def test_adapter_reproduces_percentile_rank_mean_and_is_deterministic(tmp_path):
    values = {
        "AAA": {"f1": 9.0, "f2": 1.0},
        "BBB": {"f1": 3.0, "f2": 3.0},
        "CCC": {"f1": 1.0, "f2": 2.0},
        "DDD": {"f1": 0.0, "f2": 0.0},
    }
    db, factors = _fixture(tmp_path, latest_rows=_rows("2026-08-26", values))
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)

    first = load_b16_signal_batch(db, factors, as_of=now, sell_tail_size=1)
    factors.write_text(json.dumps({"B16": [
        {"name": "f2", "expression": "expr_f2"},
        {"name": "f1", "expression": "expr_f1"},
    ]}))
    second = load_b16_signal_batch(db, factors, as_of=now, sell_tail_size=1)

    assert [row.symbol for row in first.ranking] == ["BBB", "AAA", "CCC", "DDD"]
    assert [row.score for row in first.ranking] == pytest.approx([0.875, 0.75, 0.625, 0.25])
    assert first.buy_candidates == ("BBB", "AAA", "CCC")
    assert first.sell_tail == ("DDD",)
    assert first.batch_id == second.batch_id
    assert first.factor_set_hash == second.factor_set_hash


def test_adapter_rejects_non_b16_strategy_before_reading(tmp_path):
    with pytest.raises(SignalAdapterError, match="B16"):
        load_b16_signal_batch(tmp_path / "missing.db", tmp_path / "missing.json",
                              strategy_id="B15")


@pytest.mark.parametrize("bad_value", [None, float("nan"), float("inf"), float("-inf")])
def test_adapter_fails_closed_on_non_finite_factor_data(tmp_path, bad_value):
    values = {"AAA": {"f1": 1.0}, "BBB": {"f1": bad_value}}
    db, factors = _fixture(tmp_path, factors=("f1",), latest_rows=_rows("2026-08-26", values))
    with pytest.raises(SignalAdapterError, match="finite"):
        load_b16_signal_batch(db, factors,
                              as_of=datetime(2026, 8, 27, tzinfo=timezone.utc))


def test_adapter_fails_closed_on_missing_active_factor_or_partial_cross_section(tmp_path):
    values = {"AAA": {"f1": 1.0, "f2": 2.0}, "BBB": {"f1": 2.0}}
    db, factors = _fixture(tmp_path, latest_rows=_rows("2026-08-26", values))
    with pytest.raises(SignalAdapterError, match="complete"):
        load_b16_signal_batch(db, factors,
                              as_of=datetime(2026, 8, 27, tzinfo=timezone.utc))


def test_adapter_fails_closed_when_latest_universe_is_materially_partial(tmp_path):
    prior = {f"S{i}": {"f1": float(i)} for i in range(10)}
    latest = {f"S{i}": {"f1": float(i)} for i in range(5)}
    db, factors = _fixture(tmp_path, factors=("f1",),
                           prior_rows=_rows("2026-08-25", prior),
                           latest_rows=_rows("2026-08-26", latest))
    with pytest.raises(SignalAdapterError, match="coverage"):
        load_b16_signal_batch(db, factors,
                              as_of=datetime(2026, 8, 27, tzinfo=timezone.utc))


@pytest.mark.parametrize("source_date, message", [
    ("2026-08-28", "future"),
    ("2026-08-20", "stale"),
])
def test_adapter_fails_closed_on_future_or_stale_source_date(tmp_path, source_date, message):
    db, factors = _fixture(tmp_path, factors=("f1",), latest_rows=_rows(source_date, {
        "AAA": {"f1": 1.0}, "BBB": {"f1": 2.0},
    }))
    with pytest.raises(SignalAdapterError, match=message):
        load_b16_signal_batch(db, factors,
                              as_of=datetime(2026, 8, 27, tzinfo=timezone.utc),
                              max_age_days=4)


def test_production_prepared_b16_ranking_fixture_matches_legacy_generator():
    batch = load_b16_signal_batch(
        as_of=datetime(2026, 8, 27, tzinfo=timezone.utc), sell_tail_size=4,
    )
    assert batch.source_date == "2026-08-26"
    assert batch.buy_candidates[:4] == ("DKS", "MRNA", "HOOD", "ZM")
    assert batch.sell_tail == ("AES", "TECH", "OGN", "DV")


def test_inactive_and_failure_marker_factors_are_excluded(tmp_path):
    db, factors = _fixture(tmp_path, factors=("active",), latest_rows=_rows("2026-08-26", {
        "AAA": {"active": 2.0}, "BBB": {"active": 1.0},
    }))
    factors.write_text(json.dumps({"B16": [
        {"name": "active", "expression": "X0"},
        {"name": "disabled", "expression": "X1", "active": False},
        {"name": "failure_marker", "status": "mining_failed"},
    ]}))
    batch = load_b16_signal_batch(
        db, factors, as_of=datetime(2026, 8, 27, tzinfo=timezone.utc),
        minimum_latest_coverage=0.5,
    )
    assert batch.factor_names == ("active",)
