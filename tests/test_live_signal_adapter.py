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


def _append_rows(db_path, rows):
    with sqlite3.connect(db_path) as con:
        con.executemany("INSERT INTO factor_values VALUES(?,?,?,?,?)", rows)


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


@pytest.mark.parametrize(("as_of", "expected_source"), [
    # UTC midnight is still the prior evening in New York.
    (datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc), "2026-08-26"),
    # Before and during the regular session, today's daily factors are not complete.
    (datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc), "2026-08-26"),
    (datetime(2026, 8, 27, 18, 0, tzinfo=timezone.utc), "2026-08-26"),
    (datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc), "2026-08-27"),
    # At the official close the session is complete.
    (datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc), "2026-08-27"),
    # Weekends and NYSE holidays retain the latest completed session.
    (datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc), "2026-08-28"),
    (datetime(2026, 9, 7, 16, 0, tzinfo=timezone.utc), "2026-09-04"),
    # DST changes the UTC close from 21:00 to 20:00 without changing semantics.
    (datetime(2026, 3, 9, 19, 59, tzinfo=timezone.utc), "2026-03-06"),
    (datetime(2026, 3, 9, 20, 0, tzinfo=timezone.utc), "2026-03-09"),
    # Black Friday's special close is 13:00 New York / 18:00 UTC.
    (datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc), "2026-11-25"),
    (datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc), "2026-11-27"),
])
def test_adapter_uses_only_latest_completed_nyse_session(tmp_path, as_of, expected_source):
    values = {"OLD": {"f1": 1.0}, "NEW": {"f1": 2.0}}
    dates = {
        "2026-03-06", "2026-03-09", "2026-08-26", "2026-08-27", "2026-08-28",
        "2026-09-04", "2026-09-08", "2026-11-25", "2026-11-27",
    }
    db, factors = _fixture(tmp_path, factors=("f1",))
    for source_date in sorted(dates):
        _append_rows(db, _rows(source_date, values))

    batch = load_b16_signal_batch(db, factors, as_of=as_of, max_age_days=400)

    assert batch.source_date == expected_source


def test_historical_as_of_is_immutable_after_later_rows_are_written(tmp_path):
    historical = {"HIST": {"f1": 2.0}, "OTHER": {"f1": 1.0}}
    future = {"FUTURE": {"f1": 9.0}, "OTHER": {"f1": 0.0}}
    db, factors = _fixture(
        tmp_path, factors=("f1",), latest_rows=_rows("2026-08-26", historical),
    )
    as_of = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
    before = load_b16_signal_batch(db, factors, as_of=as_of)

    _append_rows(db, _rows("2026-08-27", future))
    _append_rows(db, _rows("2026-08-28", future))
    after = load_b16_signal_batch(db, factors, as_of=as_of)

    assert before == after
    assert after.source_date == "2026-08-26"
    assert after.buy_candidates == ("HIST",)


def test_adapter_falls_back_to_older_data_within_age_limit_and_never_future(tmp_path):
    values = {"AAA": {"f1": 1.0}, "BBB": {"f1": 2.0}}
    db, factors = _fixture(
        tmp_path, factors=("f1",),
        prior_rows=_rows("2026-08-25", values),
        latest_rows=_rows("2026-08-28", values),
    )

    batch = load_b16_signal_batch(
        db, factors, as_of=datetime(2026, 8, 27, 18, 0, tzinfo=timezone.utc),
        max_age_days=1,
    )
    assert batch.source_date == "2026-08-25"

    with pytest.raises(SignalAdapterError, match="stale"):
        load_b16_signal_batch(
            db, factors, as_of=datetime(2026, 8, 27, 18, 0, tzinfo=timezone.utc),
            max_age_days=0,
        )


def test_adapter_fails_closed_when_calendar_dependency_is_unavailable(tmp_path, monkeypatch):
    values = {"AAA": {"f1": 1.0}, "BBB": {"f1": 2.0}}
    db, factors = _fixture(
        tmp_path, factors=("f1"), latest_rows=_rows("2026-08-26", values),
    )
    monkeypatch.setitem(__import__("sys").modules, "exchange_calendars", None)

    with pytest.raises(SignalAdapterError, match="NYSE calendar"):
        load_b16_signal_batch(
            db, factors, as_of=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )


def test_adapter_rejects_non_session_source_rows(tmp_path):
    values = {"AAA": {"f1": 1.0}, "BBB": {"f1": 2.0}}
    db, factors = _fixture(
        tmp_path, factors=("f1",), latest_rows=_rows("2026-09-07", values),
    )

    with pytest.raises(SignalAdapterError, match="not an NYSE session"):
        load_b16_signal_batch(
            db, factors, as_of=datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc),
        )


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
