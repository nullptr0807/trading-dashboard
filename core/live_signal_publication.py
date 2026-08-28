"""Fail-closed, append-only point-in-time publications for B16 live signals.

``eligible_at`` is deliberately not described as a commit timestamp.  It is a
future visibility boundary assigned only after source reads and expensive
validation finish.  Cooperative readers share a sidecar lock with the
publisher, so a commit that misses that boundary is revoked before readers can
observe it.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

EXPECTED_CALENDAR_VERSION = "4.13.2"
CALENDAR_NAME = "XNYS"
FACTOR_GROUP = "gp_B16"
SCHEMA_VERSION = 2
STORE_BUSY_TIMEOUT_SECONDS = 5
ELIGIBILITY_MARGIN_SECONDS = 1
ELIGIBILITY_DELAY_SECONDS = STORE_BUSY_TIMEOUT_SECONDS + ELIGIBILITY_MARGIN_SECONDS
DEFAULT_SOURCE_DB_PATH = Path("/home/gexin/quant-trading/data/trading.db")
DEFAULT_FACTORS_PATH = Path("/home/gexin/quant-trading/factors/mined_alphas_per_account.json")
DEFAULT_PUBLICATION_DB_PATH = Path(__file__).resolve().parents[1] / "data/live_signal_publications.db"


class PublicationError(RuntimeError):
    """Raised when a source or publication cannot be proven safe."""


@dataclass(frozen=True)
class PublicationResult:
    strategy_id: str
    source_date: str
    eligible_at: str | None
    version: int
    payload_sha256: str
    source_content_sha256: str
    universe_size: int
    baseline_kind: str
    baseline_date: str
    baseline_size: int
    persisted: bool


@dataclass(frozen=True)
class CoverageBaseline:
    kind: str
    baseline_date: str
    size: int
    sha256: str


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_datetime(value: datetime | None, *, label: str) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise PublicationError(f"{label} must be timezone-aware")
    return result.astimezone(timezone.utc)


def timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def active_factor_names(path: str | Path) -> tuple[str, ...]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = document["B16"]
        active = [entry for entry in entries
                  if entry.get("expression") and entry.get("active", True) is not False]
        names = tuple(str(entry["name"]).strip() for entry in active)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PublicationError("Unable to read active B16 factor set") from exc
    if not names or any(not item for item in names) or len(set(names)) != len(names):
        raise PublicationError("Active B16 factor set is empty or invalid")
    return tuple(sorted(names))


def checked_calendar() -> Any:
    try:
        installed = importlib.metadata.version("exchange-calendars")
        if installed != EXPECTED_CALENDAR_VERSION:
            raise PublicationError(
                f"exchange-calendars must be exactly {EXPECTED_CALENDAR_VERSION}; got {installed}"
            )
        import exchange_calendars as xcals
        calendar = xcals.get_calendar(CALENDAR_NAME)
        calendar.date_to_session("2026-01-02", direction="none")
        return calendar
    except PublicationError:
        raise
    except Exception as exc:
        raise PublicationError("Unable to validate XNYS calendar coverage") from exc


def latest_completed_session(now: datetime) -> tuple[Any, date]:
    try:
        import pandas as pd
        calendar = checked_calendar()
        candidate = calendar.date_to_session(pd.Timestamp(now.date()), direction="previous")
        while calendar.session_close(candidate).to_pydatetime() > now:
            candidate = calendar.previous_session(candidate)
        return calendar, candidate.date()
    except PublicationError:
        raise
    except Exception as exc:
        raise PublicationError("Unable to determine completed XNYS session") from exc


def parse_session(value: str, calendar: Any, *, label: str) -> date:
    try:
        import pandas as pd
        parsed = date.fromisoformat(value)
        if not calendar.is_session(pd.Timestamp(parsed)):
            raise PublicationError(f"{label} B16 source date is not an XNYS session")
        return parsed
    except PublicationError:
        raise
    except Exception as exc:
        raise PublicationError(f"Invalid {label.lower()} B16 source date") from exc


def average_percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    result: dict[str, float] = {}
    index = 0
    while index < count:
        end = index + 1
        while end < count and ordered[end][1] == ordered[index][1]:
            end += 1
        percentile = (((index + 1) + end) / 2.0) / count
        for position in range(index, end):
            result[ordered[position][0]] = percentile
        index = end
    return result


def ranking_from_values(
    values: list[list[Any]], factor_names: tuple[str, ...]
) -> tuple[list[list[str]], dict[str, dict[str, float]]]:
    matrix: dict[str, dict[str, float]] = {}
    for item in values:
        if not isinstance(item, list) or len(item) != 3:
            raise PublicationError("B16 publication payload is malformed")
        symbol, factor, raw_value = item
        symbol = str(symbol).strip()
        factor = str(factor)
        try:
            number = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise PublicationError("B16 factor values must be finite") from exc
        if not symbol or not math.isfinite(number):
            raise PublicationError("B16 factor values must be finite")
        if factor in matrix.setdefault(symbol, {}):
            raise PublicationError("B16 cross-section contains duplicate rows")
        matrix[symbol][factor] = number
    expected = set(factor_names)
    if not matrix or any(set(row) != expected for row in matrix.values()):
        raise PublicationError("B16 cross-section is not complete")
    if {factor for row in matrix.values() for factor in row} != expected:
        raise PublicationError("B16 factor set does not match active factor set")
    ranks = {
        factor: average_percentile_ranks({symbol: row[factor] for symbol, row in matrix.items()})
        for factor in factor_names
    }
    scored = [[symbol, format(sum(ranks[f][symbol] for f in factor_names) /
                              len(factor_names), ".17g")] for symbol in matrix]
    scored.sort(key=lambda row: (-float(row[1]), row[0]))
    return scored, matrix


def readonly_connection(path: str | Path) -> sqlite3.Connection:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        con = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        return con
    except (OSError, sqlite3.Error) as exc:
        raise PublicationError("Unable to open B16 source database read-only") from exc


def _lock_path(path: str | Path) -> Path:
    destination = Path(path).expanduser().resolve()
    return destination.with_name(destination.name + ".lock")


@contextmanager
def publication_lock(path: str | Path, *, exclusive: bool) -> Iterator[int]:
    """Serialize publishers and keep readers out until late commits are revoked."""
    lock_path = _lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + STORE_BUSY_TIMEOUT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PublicationError("Publication store lock busy timeout")
                time.sleep(0.01)
        yield descriptor
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def set_store_quarantine(descriptor: int, state: dict[str, Any] | None) -> None:
    """Durably mark an append as unresolved, or clear it after commit proof."""
    content = b"" if state is None else canonical_json(state).encode("utf-8")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    if content:
        os.write(descriptor, content)
    os.fsync(descriptor)


def require_store_not_quarantined(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.read(descriptor, 65536).strip():
        raise PublicationError("Publication store has an unresolved append quarantine")


_SCHEMA_OBJECTS = {
    ("table", "signal_publications"): """CREATE TABLE signal_publications(
        publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL CHECK(strategy_id='B16'),
        source_date TEXT NOT NULL,
        eligible_at TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version > 0),
        factor_names_json TEXT NOT NULL,
        factor_set_sha256 TEXT NOT NULL CHECK(length(factor_set_sha256)=64),
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
        source_db_path TEXT NOT NULL,
        source_content_sha256 TEXT NOT NULL CHECK(length(source_content_sha256)=64),
        calendar_name TEXT NOT NULL CHECK(calendar_name='XNYS'),
        calendar_version TEXT NOT NULL,
        universe_size INTEGER NOT NULL CHECK(universe_size > 0),
        baseline_kind TEXT NOT NULL CHECK(baseline_kind IN ('universe_membership','prior_session')),
        baseline_date TEXT NOT NULL,
        baseline_size INTEGER NOT NULL CHECK(baseline_size > 0),
        baseline_sha256 TEXT NOT NULL CHECK(length(baseline_sha256)=64),
        append_started_at TEXT NOT NULL,
        record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64),
        UNIQUE(strategy_id, source_date, version),
        UNIQUE(strategy_id, source_date, factor_set_sha256, payload_sha256)
    )""",
    ("table", "signal_publication_revocations"): """CREATE TABLE signal_publication_revocations(
        publication_id INTEGER PRIMARY KEY,
        revoked_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        FOREIGN KEY(publication_id) REFERENCES signal_publications(publication_id)
    )""",
    ("index", "idx_signal_publications_pit"): """CREATE INDEX idx_signal_publications_pit
        ON signal_publications(strategy_id, eligible_at, source_date, publication_id)""",
    ("trigger", "signal_publications_no_update"): """CREATE TRIGGER signal_publications_no_update
        BEFORE UPDATE ON signal_publications BEGIN
        SELECT RAISE(ABORT, 'signal publications are immutable'); END""",
    ("trigger", "signal_publications_no_delete"): """CREATE TRIGGER signal_publications_no_delete
        BEFORE DELETE ON signal_publications BEGIN
        SELECT RAISE(ABORT, 'signal publications are immutable'); END""",
    ("trigger", "signal_publications_no_reinsert"): """CREATE TRIGGER signal_publications_no_reinsert
        BEFORE INSERT ON signal_publications
        WHEN EXISTS(SELECT 1 FROM signal_publications AS old WHERE
            old.publication_id=NEW.publication_id OR
            (old.strategy_id=NEW.strategy_id AND old.source_date=NEW.source_date AND old.version=NEW.version) OR
            (old.strategy_id=NEW.strategy_id AND old.source_date=NEW.source_date AND
             old.factor_set_sha256=NEW.factor_set_sha256 AND old.payload_sha256=NEW.payload_sha256))
        BEGIN SELECT RAISE(ABORT, 'signal publications are immutable'); END""",
    ("trigger", "signal_revocations_no_update"): """CREATE TRIGGER signal_revocations_no_update
        BEFORE UPDATE ON signal_publication_revocations BEGIN
        SELECT RAISE(ABORT, 'signal publication revocations are immutable'); END""",
    ("trigger", "signal_revocations_no_delete"): """CREATE TRIGGER signal_revocations_no_delete
        BEFORE DELETE ON signal_publication_revocations BEGIN
        SELECT RAISE(ABORT, 'signal publication revocations are immutable'); END""",
    ("trigger", "signal_revocations_no_reinsert"): """CREATE TRIGGER signal_revocations_no_reinsert
        BEFORE INSERT ON signal_publication_revocations
        WHEN EXISTS(SELECT 1 FROM signal_publication_revocations WHERE publication_id=NEW.publication_id)
        BEGIN SELECT RAISE(ABORT, 'signal publication revocations are immutable'); END""",
}


def _create_sql(sql: str) -> str:
    return re.sub(r"(?i)^create\s+(table|index|trigger)\s+", r"CREATE \1 IF NOT EXISTS ", sql.strip()) + ";"


def _normalized_schema_sql(sql: str) -> str:
    text = re.sub(r"(?i)\bif\s+not\s+exists\b", "", sql or "")
    text = re.sub(r"\s+", " ", text.strip().rstrip(";"))
    return text.lower()


def initialize_store(path: str | Path) -> sqlite3.Connection:
    destination = Path(path).expanduser().resolve()
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(destination.parent, 0o700)
    con = sqlite3.connect(destination, timeout=STORE_BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise PublicationError(f"Unsupported publication schema version {version}")
        for sql in _SCHEMA_OBJECTS.values():
            con.execute(_create_sql(sql))
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        validate_store_schema(con)
        os.chmod(destination, 0o600)
        return con
    except Exception:
        con.close()
        raise


def validate_store_schema(con: sqlite3.Connection) -> None:
    try:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        rows = con.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name IN (%s)" %
            ",".join("?" for _ in _SCHEMA_OBJECTS),
            tuple(name for _, name in _SCHEMA_OBJECTS),
        ).fetchall()
    except sqlite3.Error as exc:
        raise PublicationError("Unable to inspect publication schema") from exc
    actual = {(str(row[0]), str(row[1])): _normalized_schema_sql(str(row[2])) for row in rows}
    expected = {key: _normalized_schema_sql(sql) for key, sql in _SCHEMA_OBJECTS.items()}
    if version != SCHEMA_VERSION or actual != expected:
        raise PublicationError("Unsupported publication schema integrity controls")


def _universe_baseline(
    con: sqlite3.Connection, source_date: str, current_symbols: set[str]
) -> CoverageBaseline | None:
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(universe_membership)")}
    required = {"market", "date", "ticker", "source", "universe_hash", "recorded_at"}
    if not required.issubset(columns):
        return None
    rows = con.execute(
        "SELECT ticker,source,universe_hash FROM universe_membership "
        "WHERE market='US' AND date=? ORDER BY ticker", (source_date,),
    ).fetchall()
    if not rows:
        return None
    sources = {str(row[1]) for row in rows}
    hashes = {str(row[2]) for row in rows}
    symbols = [str(row[0]).strip() for row in rows]
    if len(sources) != 1 or len(hashes) != 1 or not all(symbols) or len(set(symbols)) != len(symbols):
        raise PublicationError("Exact universe coverage baseline is ambiguous")
    if not current_symbols.issubset(set(symbols)):
        raise PublicationError("B16 factor symbols are outside exact universe baseline")
    evidence = {"kind": "universe_membership", "date": source_date,
                "source": next(iter(sources)), "universe_hash": next(iter(hashes)),
                "symbols": symbols}
    return CoverageBaseline("universe_membership", source_date, len(symbols), sha256_json(evidence))


def _prior_session_baseline(
    con: sqlite3.Connection, source_date: str, factor_names: tuple[str, ...], calendar: Any
) -> CoverageBaseline | None:
    previous = con.execute(
        "SELECT MAX(date) FROM factor_values WHERE factor_group=? AND date<?",
        (FACTOR_GROUP, source_date),
    ).fetchone()[0]
    if not previous:
        return None
    previous_text = str(previous)
    parse_session(previous_text, calendar, label="Previous")
    placeholders = ",".join("?" for _ in factor_names)
    rows = con.execute(
        f"SELECT ticker,factor_name,value FROM factor_values WHERE factor_group=? AND date=? "
        f"AND factor_name IN ({placeholders}) ORDER BY ticker,factor_name",
        (FACTOR_GROUP, previous_text, *factor_names),
    ).fetchall()
    normalized = [[str(row["ticker"]).strip(), str(row["factor_name"]), row["value"]]
                  for row in rows]
    if not normalized:
        return None
    _, matrix = ranking_from_values(normalized, factor_names)
    evidence = {"kind": "prior_session", "date": previous_text,
                "factor_names": factor_names, "values": normalized}
    return CoverageBaseline("prior_session", previous_text, len(matrix), sha256_json(evidence))


def _snapshot_source(
    source_db_path: str | Path,
    factor_names: tuple[str, ...],
    calendar: Any,
    cutoff_date: date,
    minimum_latest_coverage: float,
) -> tuple[str, list[list[Any]], CoverageBaseline]:
    try:
        with readonly_connection(source_db_path) as con:
            con.execute("BEGIN")
            latest = con.execute(
                "SELECT MAX(date) FROM factor_values WHERE factor_group=?", (FACTOR_GROUP,)
            ).fetchone()[0]
            if not latest:
                raise PublicationError("No gp_B16 factor values to publish")
            source_date = str(latest)
            parsed = parse_session(source_date, calendar, label="Selected")
            if parsed > cutoff_date:
                raise PublicationError("B16 source date is in the future or not completed")
            rows = con.execute(
                "SELECT ticker,factor_name,value FROM factor_values "
                "WHERE factor_group=? AND date=? ORDER BY ticker,factor_name",
                (FACTOR_GROUP, source_date),
            ).fetchall()
            normalized = [[str(row["ticker"]).strip(), str(row["factor_name"]), row["value"]]
                          for row in rows]
            _, matrix = ranking_from_values(normalized, factor_names)
            baseline = _universe_baseline(con, source_date, set(matrix))
            if baseline is None:
                baseline = _prior_session_baseline(con, source_date, factor_names, calendar)
            if baseline is None:
                raise PublicationError("No verifiable B16 coverage baseline")
            if len(matrix) / baseline.size < minimum_latest_coverage:
                raise PublicationError("Latest B16 cross-section coverage is partial")
            con.commit()
    except PublicationError:
        raise
    except sqlite3.Error as exc:
        raise PublicationError("Unable to read a consistent B16 source snapshot") from exc
    return source_date, normalized, baseline


def publication_record_material(values: dict[str, Any]) -> dict[str, Any]:
    """Canonical metadata bound by ``record_sha256`` (excluding row id/hash)."""
    keys = (
        "strategy_id", "source_date", "eligible_at", "version", "factor_names_json",
        "factor_set_sha256", "payload_json", "payload_sha256", "source_db_path",
        "source_content_sha256", "calendar_name", "calendar_version", "universe_size",
        "baseline_kind", "baseline_date", "baseline_size", "baseline_sha256",
        "append_started_at",
    )
    return {key: values[key] for key in keys}


def publish_b16_signal(
    source_db_path: str | Path = DEFAULT_SOURCE_DB_PATH,
    factors_path: str | Path = DEFAULT_FACTORS_PATH,
    publication_db_path: str | Path = DEFAULT_PUBLICATION_DB_PATH,
    *,
    clock: Callable[[], datetime] | None = None,
    publish: bool = False,
    minimum_latest_coverage: float = 0.90,
) -> PublicationResult:
    """Validate a source snapshot and optionally append an immutable publication.

    ``clock`` is test-only dependency injection and is not exposed by the CLI.
    Dry-runs have no eligibility or version because neither exists before an
    append transaction.  The session cutoff is the real invocation time; the
    eligibility clock is sampled later, after all source validation.
    """
    if not 0 < minimum_latest_coverage <= 1:
        raise PublicationError("Invalid publication coverage policy")
    get_now = clock or (lambda: datetime.now(timezone.utc))
    called_at = utc_datetime(get_now(), label="clock")
    calendar, cutoff_date = latest_completed_session(called_at)
    factors = active_factor_names(factors_path)
    source_date, values, baseline = _snapshot_source(
        source_db_path, factors, calendar, cutoff_date, minimum_latest_coverage,
    )
    ranking, matrix = ranking_from_values(values, factors)
    factor_hash = sha256_json({"strategy_id": "B16", "factor_names": factors})
    source_hash = sha256_json({
        "factor_group": FACTOR_GROUP, "source_date": source_date,
        "factor_names": factors, "values": values,
    })
    coverage_by_factor = {factor: len(matrix) / baseline.size for factor in factors}
    payload = {
        "schema_version": 2, "strategy_id": "B16", "source_date": source_date,
        "factor_names": list(factors), "factor_set_sha256": factor_hash,
        "values": values, "ranking": ranking, "universe_size": len(matrix),
        "coverage": {"baseline_kind": baseline.kind, "baseline_date": baseline.baseline_date,
                     "baseline_size": baseline.size, "baseline_sha256": baseline.sha256,
                     "by_factor": coverage_by_factor},
        "source_content_sha256": source_hash,
        "calendar": {"name": CALENDAR_NAME, "version": EXPECTED_CALENDAR_VERSION},
    }
    payload_text = canonical_json(payload)
    payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    preview = PublicationResult(
        "B16", source_date, None, 0, payload_hash, source_hash, len(matrix),
        baseline.kind, baseline.baseline_date, baseline.size, False,
    )
    if not publish:
        return preview

    with publication_lock(publication_db_path, exclusive=True) as lock_descriptor:
        require_store_not_quarantined(lock_descriptor)
        con = initialize_store(publication_db_path)
        quarantine_set = False
        commit_attempted = False
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT version,eligible_at FROM signal_publications p "
                "WHERE strategy_id='B16' AND source_date=? AND factor_set_sha256=? "
                "AND payload_sha256=? AND NOT EXISTS(SELECT 1 FROM signal_publication_revocations r "
                "WHERE r.publication_id=p.publication_id)",
                (source_date, factor_hash, payload_hash),
            ).fetchone()
            if existing:
                con.commit()
                return PublicationResult(
                    "B16", source_date, str(existing[1]), int(existing[0]), payload_hash,
                    source_hash, len(matrix), baseline.kind, baseline.baseline_date,
                    baseline.size, True,
                )
            append_started = utc_datetime(get_now(), label="clock")
            eligible = append_started + timedelta(seconds=ELIGIBILITY_DELAY_SECONDS)
            append_text = timestamp_text(append_started)
            eligible_text = timestamp_text(eligible)
            version = int(con.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM signal_publications "
                "WHERE strategy_id='B16' AND source_date=?", (source_date,),
            ).fetchone()[0])
            values_by_name: dict[str, Any] = {
                "strategy_id": "B16", "source_date": source_date, "eligible_at": eligible_text,
                "version": version, "factor_names_json": canonical_json(list(factors)),
                "factor_set_sha256": factor_hash, "payload_json": payload_text,
                "payload_sha256": payload_hash,
                "source_db_path": str(Path(source_db_path).expanduser().resolve()),
                "source_content_sha256": source_hash, "calendar_name": CALENDAR_NAME,
                "calendar_version": EXPECTED_CALENDAR_VERSION, "universe_size": len(matrix),
                "baseline_kind": baseline.kind, "baseline_date": baseline.baseline_date,
                "baseline_size": baseline.size, "baseline_sha256": baseline.sha256,
                "append_started_at": append_text,
            }
            record_hash = sha256_json(publication_record_material(values_by_name))
            set_store_quarantine(lock_descriptor, {
                "state": "append_unresolved", "eligible_at": eligible_text,
                "source_date": source_date, "version": version,
                "record_sha256": record_hash,
            })
            quarantine_set = True
            cursor = con.execute(
                """INSERT INTO signal_publications(
                    strategy_id,source_date,eligible_at,version,factor_names_json,
                    factor_set_sha256,payload_json,payload_sha256,source_db_path,
                    source_content_sha256,calendar_name,calendar_version,universe_size,
                    baseline_kind,baseline_date,baseline_size,baseline_sha256,
                    append_started_at,record_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*publication_record_material(values_by_name).values(), record_hash),
            )
            if cursor.lastrowid is None:
                raise PublicationError("Publication append did not return an id")
            publication_id = cursor.lastrowid
            commit_attempted = True
            con.commit()
            committed_returned_at = utc_datetime(get_now(), label="clock")
            if committed_returned_at >= eligible:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "INSERT INTO signal_publication_revocations(publication_id,revoked_at,reason) "
                    "VALUES(?,?,?)",
                    (publication_id, timestamp_text(committed_returned_at),
                     "append commit did not complete before eligible_at"),
                )
                con.commit()
                set_store_quarantine(lock_descriptor, None)
                quarantine_set = False
                raise PublicationError("Publication commit missed eligibility and was revoked")
            set_store_quarantine(lock_descriptor, None)
            quarantine_set = False
            return PublicationResult(
                "B16", source_date, eligible_text, version, payload_hash, source_hash,
                len(matrix), baseline.kind, baseline.baseline_date, baseline.size, True,
            )
        except PublicationError:
            if con.in_transaction:
                con.rollback()
            if quarantine_set and not commit_attempted:
                set_store_quarantine(lock_descriptor, None)
            raise
        except sqlite3.Error as exc:
            if con.in_transaction:
                con.rollback()
            if quarantine_set and not commit_attempted:
                set_store_quarantine(lock_descriptor, None)
            raise PublicationError("Unable to append immutable B16 publication") from exc
        finally:
            con.close()
