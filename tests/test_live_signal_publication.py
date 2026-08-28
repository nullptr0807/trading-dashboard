from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from core.live_signal_adapter import SignalAdapterError, load_b16_signal_batch
from core.live_signal_publication import PublicationError, publish_b16_signal


def _source(tmp_path, *, factors=("f1", "f2"), rows=()):
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
        con.executemany("INSERT INTO factor_values VALUES(?,?,?,?,?)", rows)
    return db_path, factors_path


def _rows(source_date, values):
    return [(ticker, source_date, factor, value, "gp_B16")
            for ticker, by_factor in values.items()
            for factor, value in by_factor.items()]


def _publish(source, factors, store, at, **kwargs):
    return publish_b16_signal(
        source, factors, store, published_at=at, publish=True, **kwargs,
    )


def test_publication_is_dry_run_by_default_and_real_write_is_private(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "private" / "publications.db"
    at = datetime(2026, 8, 27, tzinfo=timezone.utc)

    preview = publish_b16_signal(source, factors, store, published_at=at)
    assert preview.persisted is False
    assert preview.version == 0
    assert not store.exists()

    written = _publish(source, factors, store, at)
    assert written.persisted is True
    assert written.version == 1
    assert store.stat().st_mode & 0o777 == 0o600
    assert store.parent.stat().st_mode & 0o077 == 0


def test_historical_as_of_uses_v1_until_v2_was_actually_published(tmp_path):
    v1 = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", v1))
    store = tmp_path / "publications.db"
    t1 = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc)
    _publish(source, factors, store, t1)

    with sqlite3.connect(source) as con:
        con.execute("UPDATE factor_values SET value=10 WHERE ticker='BBB' AND date='2026-08-26'")
        con.execute("UPDATE factor_values SET value=0 WHERE ticker='AAA' AND date='2026-08-26'")
    _publish(source, factors, store, t2)

    between = load_b16_signal_batch(
        store, factors, as_of=datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc),
    )
    after = load_b16_signal_batch(
        store, factors, as_of=datetime(2026, 8, 27, 4, 0, tzinfo=timezone.utc),
    )
    assert between.buy_candidates == ("AAA",)
    assert between.publication_version == 1
    assert after.buy_candidates == ("BBB",)
    assert after.publication_version == 2
    assert between.batch_id != after.batch_id


def test_repeated_identical_publication_is_idempotent_and_concurrent_safe(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    at = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda _: _publish(source, factors, store, at), range(12),
        ))

    assert {row.version for row in results} == {1}
    with sqlite3.connect(store) as con:
        assert con.execute("SELECT COUNT(*) FROM signal_publications").fetchone()[0] == 1


def test_payload_or_metadata_tampering_is_rejected_and_rows_are_immutable(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    at = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
    _publish(source, factors, store, at)

    with sqlite3.connect(store) as con:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute("UPDATE signal_publications SET payload_sha256=?", ("0" * 64,))
        con.execute("DROP TRIGGER signal_publications_no_update")
        con.execute("UPDATE signal_publications SET payload_sha256=?", ("0" * 64,))

    with pytest.raises(SignalAdapterError, match="integrity"):
        load_b16_signal_batch(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


def test_loader_requires_publication_proof_and_current_factor_set(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    with pytest.raises(SignalAdapterError, match="publication"):
        load_b16_signal_batch(store, factors, as_of=datetime(2026, 8, 27, tzinfo=timezone.utc))

    _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    factors.write_text(json.dumps({"B16": [{"name": "f2", "expression": "X0"}]}))
    with pytest.raises(SignalAdapterError, match="factor set"):
        load_b16_signal_batch(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize("bad_value", [None, float("nan"), float("inf"), float("-inf")])
def test_publisher_rejects_nonfinite_or_incomplete_source(tmp_path, bad_value):
    values = {"AAA": {"f1": 2.0, "f2": 1.0}, "BBB": {"f1": bad_value}}
    source, factors = _source(tmp_path, rows=_rows("2026-08-26", values))
    with pytest.raises(PublicationError, match="finite|complete"):
        _publish(source, factors, tmp_path / "pub.db",
                 datetime(2026, 8, 27, tzinfo=timezone.utc))


def test_publisher_rejects_partial_future_and_non_session_source(tmp_path):
    prior = {f"S{i}": {"f1": float(i)} for i in range(10)}
    partial = {f"S{i}": {"f1": float(i)} for i in range(5)}
    source, factors = _source(
        tmp_path, factors=("f1",),
        rows=_rows("2026-08-25", prior) + _rows("2026-08-26", partial),
    )
    with pytest.raises(PublicationError, match="partial"):
        _publish(source, factors, tmp_path / "partial.db",
                 datetime(2026, 8, 27, tzinfo=timezone.utc))

    for source_date, message in (("2026-08-28", "future"), ("2026-09-07", "session")):
        case = tmp_path / source_date
        case.mkdir()
        src, fac = _source(case, factors=("f1",), rows=_rows(source_date, prior))
        with pytest.raises(PublicationError, match=message):
            _publish(src, fac, case / "pub.db",
                     datetime(2026, 8, 27, tzinfo=timezone.utc))


def test_loader_honors_published_at_and_completed_session_cutoff(tmp_path):
    old = {"OLD": {"f1": 2.0}, "X": {"f1": 1.0}}
    new = {"NEW": {"f1": 2.0}, "X": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", old))
    store = tmp_path / "publications.db"
    _publish(source, factors, store, datetime(2026, 8, 26, 21, tzinfo=timezone.utc))
    with sqlite3.connect(source) as con:
        con.executemany("INSERT INTO factor_values VALUES(?,?,?,?,?)", _rows("2026-08-27", new))
    _publish(source, factors, store, datetime(2026, 8, 27, 20, tzinfo=timezone.utc))

    before_close = load_b16_signal_batch(
        store, factors, as_of=datetime(2026, 8, 27, 19, 59, tzinfo=timezone.utc),
    )
    exact_close = load_b16_signal_batch(
        store, factors, as_of=datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc),
    )
    assert before_close.source_date == "2026-08-26"
    assert exact_close.source_date == "2026-08-27"


def test_unknown_schema_version_fails_closed(tmp_path):
    store = tmp_path / "publications.db"
    with sqlite3.connect(store) as con:
        con.execute("PRAGMA user_version=99")
    factors = tmp_path / "factors.json"
    factors.write_text(json.dumps({"B16": [{"name": "f1", "expression": "X0"}]}))
    with pytest.raises(SignalAdapterError, match="schema"):
        load_b16_signal_batch(store, factors, as_of=datetime(2026, 8, 27, tzinfo=timezone.utc))
