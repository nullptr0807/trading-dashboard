from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from core.live_signal_adapter import SignalAdapterError, load_b16_signal_batch
from core.live_signal_publication import publish_b16_signal


def _fixture(tmp_path, *, factors=("f1", "f2"), source_date="2026-08-26", values=None):
    values = values or {
        "AAA": {"f1": 9.0, "f2": 1.0},
        "BBB": {"f1": 3.0, "f2": 3.0},
        "CCC": {"f1": 1.0, "f2": 2.0},
        "DDD": {"f1": 0.0, "f2": 0.0},
    }
    factors_path = tmp_path / "factors.json"
    factors_path.write_text(json.dumps({
        "B16": [{"name": name, "expression": f"expr_{name}"} for name in factors]
    }))
    source = tmp_path / "factors.db"
    with sqlite3.connect(source) as con:
        con.execute("""CREATE TABLE factor_values(
            ticker TEXT NOT NULL, date TEXT NOT NULL, factor_name TEXT NOT NULL,
            value REAL, factor_group TEXT NOT NULL,
            PRIMARY KEY(ticker,date,factor_name,factor_group))""")
        con.executemany(
            "INSERT INTO factor_values VALUES(?,?,?,?,?)",
            [(ticker, source_date, factor, value, "gp_B16")
             for ticker, row in values.items() for factor, value in row.items()],
        )
    return source, factors_path


def _published(tmp_path, **kwargs):
    source, factors = _fixture(tmp_path, **kwargs)
    store = tmp_path / "publications.db"
    publish_b16_signal(
        source, factors, store,
        published_at=datetime(2026, 8, 27, tzinfo=timezone.utc), publish=True,
    )
    return source, factors, store


def test_adapter_reproduces_rank_mean_from_immutable_payload(tmp_path):
    _, factors, store = _published(tmp_path)
    first = load_b16_signal_batch(
        store, factors, as_of=datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
        sell_tail_size=1,
    )
    factors.write_text(json.dumps({"B16": [
        {"name": "f2", "expression": "expr_f2"},
        {"name": "f1", "expression": "expr_f1"},
    ]}))
    second = load_b16_signal_batch(
        store, factors, as_of=datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
        sell_tail_size=1,
    )
    assert [row.symbol for row in first.ranking] == ["BBB", "AAA", "CCC", "DDD"]
    assert [row.score for row in first.ranking] == pytest.approx([0.875, 0.75, 0.625, 0.25])
    assert first.buy_candidates == ("BBB", "AAA", "CCC")
    assert first.sell_tail == ("DDD",)
    assert first == second
    assert first.publication_version == 1


def test_adapter_never_treats_factor_values_database_as_publication(tmp_path):
    source, factors = _fixture(tmp_path, factors=("f1",), values={
        "AAA": {"f1": 2.0}, "BBB": {"f1": 1.0},
    })
    with pytest.raises(SignalAdapterError, match="publication schema"):
        load_b16_signal_batch(
            source, factors, as_of=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )


def test_adapter_rejects_stale_published_session(tmp_path):
    source, factors = _fixture(
        tmp_path, factors=("f1",), source_date="2026-08-20",
        values={"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}},
    )
    store = tmp_path / "publications.db"
    publish_b16_signal(
        source, factors, store,
        published_at=datetime(2026, 8, 21, tzinfo=timezone.utc), publish=True,
    )
    with pytest.raises(SignalAdapterError, match="stale"):
        load_b16_signal_batch(
            store, factors, as_of=datetime(2026, 8, 27, tzinfo=timezone.utc),
            max_age_days=4,
        )


def test_adapter_fails_closed_on_wrong_calendar_runtime_version(tmp_path, monkeypatch):
    _, factors, store = _published(tmp_path)
    import core.live_signal_publication as publication

    real_version = publication.importlib.metadata.version
    monkeypatch.setattr(
        publication.importlib.metadata, "version",
        lambda name: "4.13.1" if name == "exchange-calendars" else real_version(name),
    )
    with pytest.raises(SignalAdapterError, match="exactly 4.13.2"):
        load_b16_signal_batch(
            store, factors, as_of=datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
        )


def test_inactive_and_failure_marker_factors_are_excluded_at_publication(tmp_path):
    source, factors = _fixture(
        tmp_path, factors=("active",),
        values={"AAA": {"active": 2.0}, "BBB": {"active": 1.0}},
    )
    factors.write_text(json.dumps({"B16": [
        {"name": "active", "expression": "X0"},
        {"name": "disabled", "expression": "X1", "active": False},
        {"name": "failure_marker", "status": "mining_failed"},
    ]}))
    store = tmp_path / "publications.db"
    publish_b16_signal(
        source, factors, store,
        published_at=datetime(2026, 8, 27, tzinfo=timezone.utc), publish=True,
    )
    batch = load_b16_signal_batch(
        store, factors, as_of=datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
    )
    assert batch.factor_names == ("active",)


def test_non_b16_strategy_is_rejected_before_any_io(tmp_path):
    with pytest.raises(SignalAdapterError, match="B16"):
        load_b16_signal_batch(
            tmp_path / "missing.db", tmp_path / "missing.json", strategy_id="B15",
        )
