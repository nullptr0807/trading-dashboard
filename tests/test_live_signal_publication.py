from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import core.live_signal_publication as publication_module
from core.live_signal_adapter import SignalAdapterError, load_b16_signal_batch
from core.live_signal_publication import (
    PublicationError,
    _SimulatedPublicationCrash,
    publication_lock,
    publish_b16_signal,
    recover_publication_store,
)


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
        source, factors, store, clock=lambda: at, publish=True, test_mode=True, **kwargs,
    )


def _load(store, factors, **kwargs):
    return load_b16_signal_batch(store, factors, test_mode=True, **kwargs)


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

    between = _load(
        store, factors, as_of=datetime(2026, 8, 27, 2, 0, 7, tzinfo=timezone.utc),
    )
    after = _load(
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
        _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


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
            _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


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
        _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


def test_eligibility_is_created_after_slow_snapshot_and_precommit_as_of_is_ineligible(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    moments = iter([
        datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc),       # real call/session cutoff
        datetime(2026, 8, 27, 1, 10, tzinfo=timezone.utc),      # after slow source validation
        datetime(2026, 8, 27, 1, 10, 1, tzinfo=timezone.utc),   # commit returned
    ])
    result = publish_b16_signal(
        source, factors, store, clock=lambda: next(moments), publish=True, test_mode=True,
    )
    assert result.eligible_at == "2026-08-27T01:10:06.000000+00:00"
    with pytest.raises(SignalAdapterError, match="eligible"):
        _load(store, factors, as_of=datetime(2026, 8, 27, 1, 10, 5, tzinfo=timezone.utc))
    assert _load(
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
        publish_b16_signal(
            source, factors, store, clock=lambda: next(moments), publish=True, test_mode=True,
        )
    with pytest.raises(SignalAdapterError, match="eligible"):
        _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


def test_unresolved_append_sidecar_from_crashed_publisher_fails_closed(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    lock = store.with_name(store.name + ".lock")
    lock.write_text('{"state":"append_unresolved"}')
    with pytest.raises(SignalAdapterError, match="quarantine"):
        _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


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
        universe_hash = hashlib.sha256(
            "\n".join(sorted(f"S{i}" for i in range(100))).encode()
        ).hexdigest()
        con.executemany("INSERT INTO universe_membership VALUES('US','2026-08-26',?,'configured_universe',?,'2026-08-26T22:00:00+00:00')",
                        [(f"S{i}", universe_hash) for i in range(100)])
    with pytest.raises(PublicationError, match="partial"):
        _publish(source, factors, tmp_path / "pub.db", datetime(2026, 8, 27, tzinfo=timezone.utc))


def test_loader_requires_publication_proof_and_current_factor_set(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "publications.db"
    with pytest.raises(SignalAdapterError, match="publication"):
        _load(store, factors, as_of=datetime(2026, 8, 27, tzinfo=timezone.utc))

    _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    factors.write_text(json.dumps({"B16": [{"name": "f2", "expression": "X0"}]}))
    with pytest.raises(SignalAdapterError, match="factor set"):
        _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


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
    new = {"OLD": {"f1": 1.0}, "X": {"f1": 2.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", old))
    store = tmp_path / "publications.db"
    _publish(source, factors, store, datetime(2026, 8, 26, 21, tzinfo=timezone.utc))
    with sqlite3.connect(source) as con:
        con.executemany("INSERT INTO factor_values VALUES(?,?,?,?,?)", _rows("2026-08-27", new))
    _publish(source, factors, store, datetime(2026, 8, 27, 20, tzinfo=timezone.utc))

    before_close = _load(
        store, factors, as_of=datetime(2026, 8, 27, 19, 59, tzinfo=timezone.utc),
    )
    exact_close = _load(
        store, factors, as_of=datetime(2026, 8, 27, 20, 0, 6, tzinfo=timezone.utc),
    )
    assert before_close.source_date == "2026-08-26"
    assert exact_close.source_date == "2026-08-27"


def test_unknown_schema_version_fails_closed(tmp_path):
    store = tmp_path / "publications.db"
    with sqlite3.connect(store) as con:
        con.execute("PRAGMA user_version=99")
    os.chmod(store, 0o600)
    factors = tmp_path / "factors.json"
    factors.write_text(json.dumps({"B16": [{"name": "f1", "expression": "X0"}]}))
    with pytest.raises(SignalAdapterError, match="schema"):
        _load(store, factors, as_of=datetime(2026, 8, 27, tzinfo=timezone.utc))



def test_fallback_requires_immediately_previous_xnys_session(tmp_path):
    current = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    rows = _rows("2026-08-20", current) + _rows("2026-08-26", current)
    source, factors = _source(tmp_path, factors=("f1",), rows=rows, add_prior=False)
    with pytest.raises(PublicationError, match="previous XNYS session"):
        _publish(source, factors, tmp_path / "pub.db",
                 datetime(2026, 8, 27, tzinfo=timezone.utc))


def test_fallback_skips_weekend_and_holiday_to_immediate_session(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    rows = _rows("2026-07-02", values) + _rows("2026-07-06", values)
    source, factors = _source(tmp_path, factors=("f1",), rows=rows, add_prior=False)
    result = _publish(source, factors, tmp_path / "pub.db",
                      datetime(2026, 7, 7, tzinfo=timezone.utc))
    assert result.baseline_date == "2026-07-02"


def test_same_size_disjoint_fallback_fails_overlap_but_small_change_passes(tmp_path):
    prior = {f"S{i}": {"f1": float(i)} for i in range(10)}
    disjoint = {f"X{i}": {"f1": float(i)} for i in range(10)}
    disjoint_case = tmp_path / "disjoint"
    disjoint_case.mkdir()
    source, factors = _source(
        disjoint_case, factors=("f1",),
        rows=_rows("2026-08-25", prior) + _rows("2026-08-26", disjoint), add_prior=False,
    )
    with pytest.raises(PublicationError, match="overlap"):
        _publish(source, factors, disjoint_case / "pub.db",
                 datetime(2026, 8, 27, tzinfo=timezone.utc))

    changed = {**{f"S{i}": {"f1": float(i)} for i in range(9)}, "NEW": {"f1": 10.0}}
    case = tmp_path / "small-change"
    case.mkdir()
    os.chmod(case, 0o700)
    source, factors = _source(
        case, factors=("f1",),
        rows=_rows("2026-08-25", prior) + _rows("2026-08-26", changed), add_prior=False,
    )
    result = _publish(source, factors, case / "pub.db",
                      datetime(2026, 8, 27, tzinfo=timezone.utc))
    assert result.persisted


@pytest.mark.parametrize("crash_at,expected_retry_version", [
    ("after_quarantine", 1),
    ("after_insert", 1),
    ("after_commit", 2),
])
def test_crash_quarantine_recovery_is_repeatable_and_allows_retry(
    tmp_path, crash_at, expected_retry_version,
):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "pub.db"
    with pytest.raises(_SimulatedPublicationCrash):
        _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
                 _test_crash_at=crash_at)
    with pytest.raises(SignalAdapterError, match="quarantine"):
        _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))

    recover_publication_store(store, test_mode=True)
    recover_publication_store(store, test_mode=True)
    retried = _publish(source, factors, store, datetime(2026, 8, 27, 3, tzinfo=timezone.utc))
    assert retried.version == expected_retry_version
    assert _load(store, factors,
                 as_of=datetime(2026, 8, 27, 4, tzinfo=timezone.utc)).publication_version == expected_retry_version
    with sqlite3.connect(store) as con:
        revoked = con.execute("SELECT COUNT(*) FROM signal_publication_revocations").fetchone()[0]
    assert revoked == (1 if crash_at == "after_commit" else 0)


def test_revoked_late_commit_can_retry_identical_content_as_new_version(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "pub.db"
    moments = iter([
        datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 1, 0, 7, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 1, 0, 7, tzinfo=timezone.utc),
    ])
    with pytest.raises(PublicationError, match="revoked"):
        publish_b16_signal(source, factors, store, clock=lambda: next(moments),
                           publish=True, test_mode=True)
    retry = _publish(source, factors, store, datetime(2026, 8, 27, 2, tzinfo=timezone.utc))
    assert retry.version == 2


def test_live_api_rejects_noncanonical_paths_even_if_source_is_valid(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    with pytest.raises(PublicationError, match="canonical"):
        publish_b16_signal(source, factors, tmp_path / "pub.db",
                           clock=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc), publish=True)
    with pytest.raises(SignalAdapterError, match="canonical"):
        load_b16_signal_batch(tmp_path / "pub.db", factors,
                              as_of=datetime(2026, 8, 27, tzinfo=timezone.utc))


def test_cli_custom_paths_are_dry_run_only(tmp_path):
    import subprocess
    import sys
    command = [sys.executable, "scripts/publish_b16_live_signal.py", "--publish",
               "--source-db", str(tmp_path / "alternate.db")]
    result = subprocess.run(command, cwd=str(__import__('pathlib').Path(__file__).parents[1]),
                            text=True, capture_output=True, check=False)
    assert result.returncode == 2
    assert "canonical" in result.stderr


@pytest.mark.parametrize("target,mode", [("parent", 0o777), ("db", 0o666), ("lock", 0o666)])
def test_permission_drift_fails_closed_immediately(tmp_path, target, mode):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    private = tmp_path / "private"
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = private / "pub.db"
    _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    path = {"parent": private, "db": store, "lock": store.with_name(store.name + ".lock")}[target]
    os.chmod(path, mode)
    with pytest.raises(SignalAdapterError, match="permission"):
        _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


def test_owner_drift_fails_closed(tmp_path, monkeypatch):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "pub.db"
    _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(os, "getuid", lambda: store.stat().st_uid + 1)
    with pytest.raises(SignalAdapterError, match="owner"):
        _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize("second_at", [
    datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
    datetime(2026, 8, 27, 0, 59, 59, tzinfo=timezone.utc),
])
def test_new_version_rejects_equal_or_rolled_back_wall_clock(tmp_path, second_at):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "pub.db"
    _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    with sqlite3.connect(source) as con:
        con.execute("UPDATE factor_values SET value=-1 WHERE ticker='AAA' AND date='2026-08-26'")
    with pytest.raises(PublicationError, match="monotonic"):
        _publish(source, factors, store, second_at)
    with sqlite3.connect(store) as con:
        assert con.execute("SELECT COUNT(*) FROM signal_publications").fetchone()[0] == 1


def test_concurrent_new_version_keeps_monotonic_time_and_idempotency(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "pub.db"
    _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    with sqlite3.connect(source) as con:
        con.execute("UPDATE factor_values SET value=-1 WHERE ticker='AAA' AND date='2026-08-26'")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda _: _publish(source, factors, store,
                               datetime(2026, 8, 27, 2, tzinfo=timezone.utc)), range(8)))
    assert {result.version for result in results} == {2}
    with sqlite3.connect(store) as con:
        times = con.execute("SELECT append_started_at FROM signal_publications ORDER BY version").fetchall()
    assert times[0][0] < times[1][0]


def test_first_publication_rejects_intra_invocation_clock_rollback(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "pub.db"
    moments = iter([
        datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 26, 23, 50, tzinfo=timezone.utc),
    ])
    with pytest.raises(PublicationError, match="backwards"):
        publish_b16_signal(source, factors, store, clock=lambda: next(moments),
                           publish=True, test_mode=True)
    with pytest.raises(SignalAdapterError, match="eligible|publication"):
        _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


def test_post_commit_clock_rollback_quarantines_publication(tmp_path):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    store = tmp_path / "pub.db"
    moments = iter([
        datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 0, 59, tzinfo=timezone.utc),
    ])
    with pytest.raises(PublicationError, match="backwards"):
        publish_b16_signal(source, factors, store, clock=lambda: next(moments),
                           publish=True, test_mode=True)
    with pytest.raises(SignalAdapterError, match="quarantine"):
        _load(store, factors, as_of=datetime(2026, 8, 27, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    "mutation", ["source", "hash", "future", "predate", "offset", "malformed", "mixed"],
)
def test_exact_universe_baseline_requires_trusted_pit_metadata(tmp_path, mutation):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(
        tmp_path, factors=("f1",), rows=_rows("2026-08-26", values), add_prior=False,
    )
    symbols = sorted(values)
    digest = hashlib.sha256("\n".join(symbols).encode()).hexdigest()
    metadata = {
        "source": "configured_universe",
        "hash": digest,
        "recorded": "2026-08-26T22:00:00+00:00",
    }
    if mutation == "source":
        metadata["source"] = "ATTACKER_SOURCE"
    elif mutation == "hash":
        metadata["hash"] = "0" * 64
    elif mutation == "future":
        metadata["recorded"] = "2026-08-28T00:00:00+00:00"
    elif mutation == "predate":
        metadata["recorded"] = "2026-08-25T23:59:59+00:00"
    elif mutation == "offset":
        metadata["recorded"] = "2026-08-27T06:00:00+08:00"
    elif mutation == "malformed":
        metadata["recorded"] = "not-a-timestamp"
    with sqlite3.connect(source) as con:
        con.execute("CREATE TABLE universe_membership(market TEXT,date TEXT,ticker TEXT,source TEXT,universe_hash TEXT,recorded_at TEXT,PRIMARY KEY(market,date,ticker))")
        rows = [
            ("US", "2026-08-26", symbol, metadata["source"], metadata["hash"],
             metadata["recorded"])
            for symbol in symbols
        ]
        if mutation == "mixed":
            rows[1] = (*rows[1][:-1], "2026-08-26T22:01:00+00:00")
        con.executemany("INSERT INTO universe_membership VALUES(?,?,?,?,?,?)", rows)
    with pytest.raises(PublicationError, match="universe coverage baseline"):
        _publish(source, factors, tmp_path / "pub.db",
                 datetime(2026, 8, 27, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize("target", ["db", "lock", "parent"])
def test_publication_store_symlinks_fail_closed(tmp_path, target):
    values = {"AAA": {"f1": 2.0}, "BBB": {"f1": 1.0}}
    source, factors = _source(tmp_path, factors=("f1",), rows=_rows("2026-08-26", values))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    store = private / "pub.db"
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    if target == "db":
        store.symlink_to(victim)
    elif target == "lock":
        store.with_name(store.name + ".lock").symlink_to(victim)
    else:
        private.rmdir()
        private.symlink_to(tmp_path)
    with pytest.raises(PublicationError, match="unsafe|safely"):
        _publish(source, factors, store, datetime(2026, 8, 27, 1, tzinfo=timezone.utc))
    assert victim.read_text() == "untouched"


def test_replaced_lock_path_is_detected_without_split_brain(tmp_path):
    store = tmp_path / "pub.db"
    lock = store.with_name(store.name + ".lock")
    entered_replacement = []
    replacement_started = threading.Event()

    def acquire_replacement():
        replacement_started.set()
        with publication_lock(store, exclusive=True):
            entered_replacement.append(True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = None
        with pytest.raises(PublicationError, match="replaced"):
            with publication_lock(store, exclusive=True):
                lock.rename(lock.with_suffix(".old"))
                lock.touch(mode=0o600)
                future = pool.submit(acquire_replacement)
                assert replacement_started.wait(timeout=1)
                time.sleep(0.05)
                assert not future.done()
                assert not entered_replacement
        assert future is not None
        future.result(timeout=2)
    assert entered_replacement == [True]


def test_store_inode_replacement_during_sqlite_open_fails_closed(tmp_path, monkeypatch):
    store = tmp_path / "pub.db"
    displaced = tmp_path / "displaced.db"
    real_connect = publication_module.sqlite3.connect

    def replace_before_connect(*args, **kwargs):
        if str(args[0]).startswith("file:///proc/self/fd/"):
            store.rename(displaced)
            store.touch(mode=0o600)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(publication_module.sqlite3, "connect", replace_before_connect)
    with pytest.raises(PublicationError, match="database path was replaced"):
        publication_module.initialize_store(store)


def test_in_process_lock_wait_has_one_bounded_deadline(tmp_path, monkeypatch):
    store = tmp_path / "pub.db"
    monkeypatch.setattr(publication_module, "STORE_BUSY_TIMEOUT_SECONDS", 0.1)

    def contend():
        with pytest.raises(PublicationError, match="busy timeout"):
            with publication_lock(store, exclusive=True):
                pass

    started = time.monotonic()
    with publication_lock(store, exclusive=True):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contend).result(timeout=1)
    assert time.monotonic() - started < 1
