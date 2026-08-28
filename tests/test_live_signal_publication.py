from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from core.live_signal_adapter import SignalAdapterError, load_b16_signal_batch
from core.live_signal_publication import PublicationError, publish_b16_signal


def _source(tmp_path, *, factors=("f1", "f2"), rows=(), add_prior=True):
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
        materialized = list(rows)
        if add_prior and materialized and len({row[1] for row in materialized}) == 1:
            latest = datetime.fromisoformat(materialized[0][1]).date()
            prior = latest - timedelta(days=1)
            while prior.weekday() >= 5:
                prior -= timedelta(days=1)
            materialized += [(ticker, prior.isoformat(), factor, value, group)
                             for ticker, _, factor, value, group in materialized]
        con.executemany("INSERT INTO factor_values VALUES(?,?,?,?,?)", materialized)
    return db_path, factors_path


def _rows(source_date, values):
    return [(ticker, source_date, factor, value, "gp_B16")
            for ticker, by_factor in values.items()
            for factor, value in by_factor.items()]


def _publish(source, factors, store, at, **kwargs):
    return publish_b16_signal(
        source, factors, store, clock=lambda: at, publish=True, **kwargs,
    )


def test_publication_is_dry_run_by_default_and_real_write_is_private(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "private" / "publications.db"
    at = datetime(2026, 8, 27, tzinfo=timezone.utc)

    preview = publish_b16_signal(source, factors, store, clock=lambda: at)
    assert preview.persisted is False
    assert preview.version == 0
    assert preview.eligible_at is None
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
        store, factors, as_of=datetime(2026, 8, 27, 2, 0, 7, tzinfo=timezone.utc),
    )
    after = load_b16_signal_batch(
        store, factors, as_of=datetime(2026, 8, 27, 4, 0, 7, tzinfo=timezone.utc),
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


def test_insert_or_replace_cannot_replace_existing_id_version_or_metadata(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    with sqlite3.connect(store) as con:
        con.execute("PRAGMA recursive_triggers=OFF")
        columns = [row[1] for row in con.execute("PRAGMA table_info(signal_publications)")]
        original = con.execute("SELECT * FROM signal_publications").fetchone()
        for field, replacement in (("version", 99), ("eligible_at", "2020-01-01T00:00:00.000000+00:00")):
            changed = list(original)
            changed[columns.index(field)] = replacement
            placeholders = ",".join("?" for _ in columns)
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                con.execute(f"INSERT OR REPLACE INTO signal_publications VALUES({placeholders})", changed)


def test_schema_validation_rejects_noop_trigger_and_canonical_schema_tampering(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    for mutation in ("noop", "index", "table"):
        store = tmp_path / f"{mutation}.db"
        _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
        with sqlite3.connect(store) as con:
            if mutation == "noop":
                con.execute("DROP TRIGGER signal_publications_no_update")
                con.execute("CREATE TRIGGER signal_publications_no_update BEFORE UPDATE ON signal_publications BEGIN SELECT 1; END")
            else:
                if mutation == "index":
                    con.execute("DROP INDEX idx_signal_publications_pit")
                    con.execute("CREATE INDEX idx_signal_publications_pit ON signal_publications(source_date)")
                else:
                    con.execute("PRAGMA writable_schema=ON")
                    con.execute("UPDATE sqlite_master SET sql=replace(sql,'version > 0','version >= 0') WHERE type='table' AND name='signal_publications'")
                    con.execute("PRAGMA writable_schema=OFF")
        with pytest.raises(SignalAdapterError, match="schema integrity"):
            load_b16_signal_batch(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize("field,replacement", [
    ("version", 99),
    ("eligible_at", "2026-08-27T00:00:00.000000+00:00"),
])
def test_record_hash_rejects_version_or_eligibility_tampering(tmp_path, field, replacement):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    with sqlite3.connect(store) as con:
        trigger_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='signal_publications_no_update'"
        ).fetchone()[0]
        con.execute("DROP TRIGGER signal_publications_no_update")
        con.execute(f"UPDATE signal_publications SET {field}=?", (replacement,))
        con.execute(trigger_sql)
    with pytest.raises(SignalAdapterError, match="integrity|eligibility"):
        load_b16_signal_batch(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


def test_eligibility_is_created_after_slow_snapshot_and_precommit_as_of_is_ineligible(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    moments = iter([
        datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc),       # real call/session cutoff
        datetime(2026, 8, 27, 1, 10, tzinfo=timezone.utc),      # after slow source validation
        datetime(2026, 8, 27, 1, 10, 1, tzinfo=timezone.utc),   # commit returned
    ])
    result = publish_b16_signal(source, factors, store, clock=lambda: next(moments), publish=True)
    assert result.eligible_at == "2026-08-27T01:10:06.000000+00:00"
    with pytest.raises(SignalAdapterError, match="eligible"):
        load_b16_signal_batch(store, factors, as_of=datetime(2026, 8, 27, 1, 10, 5, tzinfo=timezone.utc))
    assert load_b16_signal_batch(
        store, factors, as_of=datetime(2026, 8, 27, 1, 10, 6, tzinfo=timezone.utc)
    ).publication_version == 1


def test_commit_finishing_after_eligibility_is_revoked_and_never_loadable(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    moments = iter([
        datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 1, 10, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 1, 10, 7, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 1, 10, 7, tzinfo=timezone.utc),
    ])
    with pytest.raises(PublicationError, match="eligibility"):
        publish_b16_signal(source, factors, store, clock=lambda: next(moments), publish=True)
    with pytest.raises(SignalAdapterError, match="eligible"):
        load_b16_signal_batch(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


def test_unresolved_append_sidecar_from_crashed_publisher_fails_closed(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    lock = store.with_name(store.name + ".lock")
    lock.write_text('{"state":"append_unresolved"}')
    with pytest.raises(SignalAdapterError, match="quarantine"):
        load_b16_signal_batch(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


def test_first_snapshot_without_exact_universe_or_prior_session_fails_closed(tmp_path):
    values = {"ONLY": {"f1": 1.0}}
    source, factors = _source(
        tmp_path, factors=("f1",), rows=_rows("2026-08-26", values), add_prior=False,
    )
    with pytest.raises(PublicationError, match="coverage baseline"):
        _publish(source, factors, tmp_path / "pub.db", datetime(2026, 8, 27, tzinfo=timezone.utc))


def test_exact_universe_baseline_rejects_partial_first_snapshot_and_large_rank_exits(tmp_path):
    current = {f"S{i}": {"f1": float(i)} for i in range(5)}
    source, factors = _source(
        tmp_path, factors=("f1",), rows=_rows("2026-08-26", current), add_prior=False,
    )
    with sqlite3.connect(source) as con:
        con.execute("CREATE TABLE universe_membership(market TEXT,date TEXT,ticker TEXT,source TEXT,universe_hash TEXT,recorded_at TEXT,PRIMARY KEY(market,date,ticker))")
        con.executemany("INSERT INTO universe_membership VALUES('US','2026-08-26',?,'configured_universe','h','2026-08-26T22:00:00+00:00')",
                        [(f"S{i}",) for i in range(100)])
    with pytest.raises(PublicationError, match="partial"):
        _publish(source, factors, tmp_path / "pub.db", datetime(2026, 8, 27, tzinfo=timezone.utc))


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


def test_loader_honors_eligibility_and_completed_session_cutoff(tmp_path):
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
        store, factors, as_of=datetime(2026, 8, 27, 20, 0, 6, tzinfo=timezone.utc),
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
