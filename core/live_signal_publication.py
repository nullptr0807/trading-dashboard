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
import stat as stat_module
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

EXPECTED_CALENDAR_VERSION = "4.13.2"
CALENDAR_NAME = "XNYS"
FACTOR_GROUP = "gp_B16"
SCHEMA_VERSION = 3
STORE_BUSY_TIMEOUT_SECONDS = 5
ELIGIBILITY_MARGIN_SECONDS = 1
ELIGIBILITY_DELAY_SECONDS = STORE_BUSY_TIMEOUT_SECONDS + ELIGIBILITY_MARGIN_SECONDS
TRUSTED_UNIVERSE_SOURCES = frozenset({"configured_universe"})
UNIVERSE_BASELINE_MAX_AGE = timedelta(days=7)
DEFAULT_SOURCE_DB_PATH = Path("/home/gexin/quant-trading/data/trading.db")
DEFAULT_FACTORS_PATH = Path("/home/gexin/quant-trading/factors/mined_alphas_per_account.json")
DEFAULT_PUBLICATION_DB_PATH = (
    Path(__file__).resolve().parents[1] / "data/signal_publication/live_signal_publications.db"
)


class PublicationError(RuntimeError):
    """Raised when a source or publication cannot be proven safe."""


class _SimulatedPublicationCrash(BaseException):
    """Test-only abrupt process-death simulation (deliberately bypasses Exception)."""


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
    symbols: tuple[str, ...]


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


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _store_path(path: str | Path) -> Path:
    """Return an absolute store path without following its final symlink."""
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def require_canonical_paths(
    source_db_path: str | Path,
    factors_path: str | Path,
    publication_db_path: str | Path,
) -> None:
    expected = (DEFAULT_SOURCE_DB_PATH, DEFAULT_FACTORS_PATH, DEFAULT_PUBLICATION_DB_PATH)
    actual = (source_db_path, factors_path, publication_db_path)
    if any(_resolved(value) != _resolved(canonical)
           for value, canonical in zip(actual, expected, strict=True)):
        raise PublicationError("Live signal publication requires canonical source, factors, and store paths")


def _require_owned_mode(path: Path, *, directory: bool) -> None:
    try:
        stat = path.lstat()
    except OSError as exc:
        raise PublicationError("Unable to verify publication store permissions") from exc
    expected_type = stat_module.S_ISDIR if directory else stat_module.S_ISREG
    if not expected_type(stat.st_mode):
        raise PublicationError("Publication store path type is unsafe")
    if stat.st_uid != os.getuid():
        raise PublicationError("Publication store owner does not match current uid")
    forbidden = 0o022 if directory else 0o077
    if stat.st_mode & forbidden:
        raise PublicationError("Publication store permissions are not restrictive")


def validate_store_permissions(path: str | Path, *, store_may_be_missing: bool = False) -> None:
    destination = _store_path(path)
    if not destination.parent.exists():
        if store_may_be_missing:
            return
        raise PublicationError("publication store parent is missing")
    _require_owned_mode(destination.parent, directory=True)
    if destination.exists() or destination.is_symlink():
        _require_owned_mode(destination, directory=False)
    elif not store_may_be_missing:
        raise PublicationError("publication store is missing")
    lock_path = _lock_path(destination)
    if lock_path.exists() or lock_path.is_symlink():
        _require_owned_mode(lock_path, directory=False)


def _lock_path(path: str | Path) -> Path:
    destination = _store_path(path)
    return destination.with_name(destination.name + ".lock")


def _ensure_private_store_parent(destination: Path) -> None:
    """Create the store parent without traversing symlink path components."""
    parent = destination.parent
    current = Path(parent.anchor)
    try:
        for component in parent.parts[1:]:
            current /= component
            try:
                item = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    # A concurrent bootstrap won mkdir; validate what appeared.
                    pass
                item = current.lstat()
            if not stat_module.S_ISDIR(item.st_mode):
                raise PublicationError("Publication store parent path type is unsafe")
    except PublicationError:
        raise
    except OSError as exc:
        raise PublicationError("Unable to create private publication store parent") from exc
    _require_owned_mode(parent, directory=True)


def _require_descriptor_path(
    path: Path, descriptor: int, *, label: str = "publication store lock"
) -> None:
    """Prove an opened descriptor still names the same regular file."""
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
    except OSError as exc:
        raise PublicationError(f"Unable to verify {label}") from exc
    if (not stat_module.S_ISREG(opened.st_mode)
            or not stat_module.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
        raise PublicationError(f"{label.capitalize()} path was replaced")


class _VerifiedStoreConnection(sqlite3.Connection):
    """SQLite connection bound to the inode securely opened by this module."""

    _store_path: Path
    _store_identity: tuple[int, int]


def _require_connection_path(con: sqlite3.Connection) -> None:
    if not isinstance(con, _VerifiedStoreConnection):
        raise PublicationError("Publication store connection is unverifiable")
    try:
        named = con._store_path.lstat()
    except OSError as exc:
        raise PublicationError("Unable to verify publication store database") from exc
    if (not stat_module.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != con._store_identity):
        raise PublicationError("Publication store database path was replaced")


def _connect_open_store(
    destination: Path, descriptor: int, *, readonly: bool
) -> _VerifiedStoreConnection:
    """Connect SQLite through a no-follow fd, then bind it to the named inode."""
    _require_descriptor_path(destination, descriptor, label="publication store database")
    mode = "ro" if readonly else "rw"
    uri = Path(f"/proc/self/fd/{descriptor}").as_uri() + f"?mode={mode}"
    try:
        con = sqlite3.connect(
            uri, uri=True, timeout=STORE_BUSY_TIMEOUT_SECONDS, isolation_level=None,
            factory=_VerifiedStoreConnection,
        )
    except sqlite3.Error as exc:
        raise PublicationError("Unable to safely open publication store") from exc
    opened = os.fstat(descriptor)
    con._store_path = destination
    con._store_identity = (opened.st_dev, opened.st_ino)
    try:
        _require_descriptor_path(destination, descriptor, label="publication store database")
        _require_connection_path(con)
    except Exception:
        con.close()
        raise
    return con


def open_store_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an existing store without following a replaced final symlink."""
    destination = _store_path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags)
    except OSError as exc:
        raise PublicationError("Unable to safely open publication store") from exc
    try:
        return _connect_open_store(destination, descriptor, readonly=True)
    finally:
        os.close(descriptor)


_process_lock_guard = threading.Lock()
_process_locks: dict[str, Any] = {}


def _process_lock(path: Path) -> Any:
    with _process_lock_guard:
        return _process_locks.setdefault(os.fspath(path), threading.Lock())


@contextmanager
def publication_lock(path: str | Path, *, exclusive: bool) -> Iterator[int]:
    """Serialize publishers and keep readers out until late commits are revoked."""
    destination = _store_path(path)
    lock_path = _lock_path(destination)
    process_lock = _process_lock(lock_path)
    deadline = time.monotonic() + STORE_BUSY_TIMEOUT_SECONDS
    if not process_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise PublicationError("Publication store lock busy timeout")
    try:
        _ensure_private_store_parent(destination)
        flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(lock_path, flags, 0o600)
    except PublicationError:
        process_lock.release()
        raise
    except OSError as exc:
        process_lock.release()
        raise PublicationError("Unable to safely open publication store lock") from exc
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        stat = os.fstat(descriptor)
        if (not stat_module.S_ISREG(stat.st_mode)
                or stat.st_uid != os.getuid() or stat.st_mode & 0o077):
            raise PublicationError("Publication store lock owner or permissions are unsafe")
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PublicationError("Publication store lock busy timeout")
                time.sleep(0.01)
        # Store validation belongs after the lock: another first publisher may
        # be creating it.  Existing permission drift still fails closed here.
        _require_descriptor_path(lock_path, descriptor)
        _require_owned_mode(destination.parent, directory=True)
        if destination.exists() or destination.is_symlink():
            _require_owned_mode(destination, directory=False)
        yield descriptor
    finally:
        try:
            _require_descriptor_path(lock_path, descriptor)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
                process_lock.release()


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


def _quarantine_state(descriptor: int) -> dict[str, Any] | None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, 65536).strip()
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError("Publication append quarantine is malformed") from exc
    required = {"state", "source_date", "version", "record_sha256"}
    if not isinstance(state, dict) or not required.issubset(state):
        raise PublicationError("Publication append quarantine is unverifiable")
    return state


def _recover_locked(path: str | Path, descriptor: int) -> None:
    state = _quarantine_state(descriptor)
    if state is None:
        return
    con = initialize_store(path)
    try:
        _require_descriptor_path(_lock_path(path), descriptor)
        _require_connection_path(con)
        validate_store_schema(con)
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM signal_publications WHERE strategy_id='B16' "
            "AND source_date=? AND version=?",
            (str(state["source_date"]), int(state["version"])),
        ).fetchone()
        if row is None:
            # BEGIN IMMEDIATE plus the process-wide exclusive lock proves absence.
            _require_descriptor_path(_lock_path(path), descriptor)
            _require_connection_path(con)
            con.commit()
            _require_descriptor_path(_lock_path(path), descriptor)
            _require_connection_path(con)
            set_store_quarantine(descriptor, None)
            return
        columns = [str(item[1]) for item in con.execute("PRAGMA table_info(signal_publications)")]
        material = dict(zip(columns, row, strict=True))
        record_valid = (
            material.get("record_sha256") == state["record_sha256"]
            and sha256_json(publication_record_material(material)) == material.get("record_sha256")
            and hashlib.sha256(str(material["payload_json"]).encode("utf-8")).hexdigest()
            == material.get("payload_sha256")
        )
        reason = ("recovered committed append with uncertain acknowledgement"
                  if record_valid else "recovered unverifiable committed append")
        existing_revocation = con.execute(
            "SELECT 1 FROM signal_publication_revocations WHERE publication_id=?",
            (int(material["publication_id"]),),
        ).fetchone()
        if existing_revocation is None:
            con.execute(
                "INSERT INTO signal_publication_revocations(publication_id,revoked_at,reason) "
                "VALUES(?,?,?)",
                (int(material["publication_id"]), timestamp_text(datetime.now(timezone.utc)), reason),
            )
        _require_descriptor_path(_lock_path(path), descriptor)
        _require_connection_path(con)
        con.commit()
        _require_descriptor_path(_lock_path(path), descriptor)
        _require_connection_path(con)
        set_store_quarantine(descriptor, None)
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def recover_publication_store(
    publication_db_path: str | Path = DEFAULT_PUBLICATION_DB_PATH, *, test_mode: bool = False
) -> None:
    """Resolve a crashed append under the exclusive lock; readers never invoke this."""
    if not test_mode and _resolved(publication_db_path) != _resolved(DEFAULT_PUBLICATION_DB_PATH):
        raise PublicationError("Live recovery requires the canonical publication store")
    with publication_lock(publication_db_path, exclusive=True) as descriptor:
        _recover_locked(publication_db_path, descriptor)


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
        baseline_symbols_json TEXT NOT NULL,
        overlap_size INTEGER NOT NULL CHECK(overlap_size > 0),
        overlap_sha256 TEXT NOT NULL CHECK(length(overlap_sha256)=64),
        append_started_at TEXT NOT NULL,
        record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64),
        UNIQUE(strategy_id, source_date, version)
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
             old.factor_set_sha256=NEW.factor_set_sha256 AND old.payload_sha256=NEW.payload_sha256 AND
             NOT EXISTS(SELECT 1 FROM signal_publication_revocations revoked
                        WHERE revoked.publication_id=old.publication_id)) OR
            (old.strategy_id=NEW.strategy_id AND
             (NEW.append_started_at<=old.append_started_at OR NEW.eligible_at<=old.eligible_at)))
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
    destination = _store_path(path)
    _ensure_private_store_parent(destination)
    validate_store_permissions(destination, store_may_be_missing=True)
    create_flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(destination, create_flags, 0o600)
    except FileExistsError:
        # Never repair an existing file: permission drift must fail closed.
        _require_owned_mode(destination, directory=False)
        open_flags = (os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
                      | getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(destination, open_flags)
        except OSError as exc:
            raise PublicationError("Unable to safely open publication store") from exc
    except OSError as exc:
        raise PublicationError("Unable to safely create publication store") from exc
    try:
        con = _connect_open_store(destination, descriptor, readonly=False)
    finally:
        os.close(descriptor)
    try:
        validate_store_permissions(destination)
        _require_connection_path(con)
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise PublicationError(f"Unsupported publication schema version {version}")
        for sql in _SCHEMA_OBJECTS.values():
            _require_connection_path(con)
            con.execute(_create_sql(sql))
        _require_connection_path(con)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        _require_connection_path(con)
        validate_store_schema(con)
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
    con: sqlite3.Connection, source_date: str, current_symbols: set[str], called_at: datetime
) -> CoverageBaseline | None:
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(universe_membership)")}
    required = {"market", "date", "ticker", "source", "universe_hash", "recorded_at"}
    if not required.issubset(columns):
        return None
    rows = con.execute(
        "SELECT ticker,source,universe_hash,recorded_at FROM universe_membership "
        "WHERE market='US' AND date=? ORDER BY ticker", (source_date,),
    ).fetchall()
    if not rows:
        return None
    sources = {str(row[1]) for row in rows}
    hashes = {str(row[2]) for row in rows}
    recorded_values = {str(row[3]) for row in rows}
    symbols = [str(row[0]).strip() for row in rows]
    if (len(sources) != 1 or len(hashes) != 1 or len(recorded_values) != 1
            or not all(symbols) or len(set(symbols)) != len(symbols)):
        raise PublicationError("Exact universe coverage baseline is ambiguous")
    source = next(iter(sources))
    if source not in TRUSTED_UNIVERSE_SOURCES:
        raise PublicationError("Exact universe coverage baseline source is untrusted")
    expected_hash = hashlib.sha256("\n".join(sorted(symbols)).encode("utf-8")).hexdigest()
    if next(iter(hashes)) != expected_hash:
        raise PublicationError("Exact universe coverage baseline hash is invalid")
    try:
        recorded_at = datetime.fromisoformat(next(iter(recorded_values)).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationError("Exact universe coverage baseline recorded_at is invalid") from exc
    if recorded_at.tzinfo is None or recorded_at.utcoffset() != timedelta(0):
        raise PublicationError("Exact universe coverage baseline recorded_at must be UTC")
    recorded_at = recorded_at.astimezone(timezone.utc)
    source_midnight = datetime.combine(date.fromisoformat(source_date), datetime.min.time(), timezone.utc)
    if (recorded_at < source_midnight or recorded_at > called_at
            or called_at - recorded_at > UNIVERSE_BASELINE_MAX_AGE):
        raise PublicationError("Exact universe coverage baseline recorded_at violates PIT freshness")
    if not current_symbols.issubset(set(symbols)):
        raise PublicationError("B16 factor symbols are outside exact universe baseline")
    evidence = {"kind": "universe_membership", "date": source_date,
                "source": source, "universe_hash": expected_hash,
                "recorded_at": timestamp_text(recorded_at),
                "symbols": symbols}
    return CoverageBaseline(
        "universe_membership", source_date, len(symbols), sha256_json(evidence), tuple(symbols)
    )


def _prior_session_baseline(
    con: sqlite3.Connection, source_date: str, factor_names: tuple[str, ...], calendar: Any
) -> CoverageBaseline | None:
    try:
        import pandas as pd
        previous_text = calendar.previous_session(pd.Timestamp(source_date)).date().isoformat()
    except Exception as exc:
        raise PublicationError("Unable to determine immediately previous XNYS session") from exc
    placeholders = ",".join("?" for _ in factor_names)
    rows = con.execute(
        f"SELECT ticker,factor_name,value FROM factor_values WHERE factor_group=? AND date=? "
        f"AND factor_name IN ({placeholders}) ORDER BY ticker,factor_name",
        (FACTOR_GROUP, previous_text, *factor_names),
    ).fetchall()
    normalized = [[str(row["ticker"]).strip(), str(row["factor_name"]), row["value"]]
                  for row in rows]
    if not normalized:
        raise PublicationError(
            "No verifiable coverage baseline for immediately previous XNYS session"
        )
    _, matrix = ranking_from_values(normalized, factor_names)
    evidence = {"kind": "prior_session", "date": previous_text,
                "factor_names": factor_names, "values": normalized}
    return CoverageBaseline(
        "prior_session", previous_text, len(matrix), sha256_json(evidence), tuple(sorted(matrix))
    )


def _snapshot_source(
    source_db_path: str | Path,
    factor_names: tuple[str, ...],
    calendar: Any,
    cutoff_date: date,
    minimum_latest_coverage: float,
    called_at: datetime,
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
            baseline = _universe_baseline(con, source_date, set(matrix), called_at)
            if baseline is None:
                baseline = _prior_session_baseline(con, source_date, factor_names, calendar)
            if baseline is None:
                raise PublicationError("No verifiable B16 coverage baseline")
            overlap = set(matrix).intersection(baseline.symbols)
            if (len(matrix) / baseline.size < minimum_latest_coverage
                    or len(overlap) / baseline.size < minimum_latest_coverage
                    or len(overlap) / len(matrix) < minimum_latest_coverage):
                if baseline.kind == "prior_session" and not overlap:
                    raise PublicationError("Latest B16 cross-section has no prior-session symbol overlap")
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
        "baseline_symbols_json", "overlap_size", "overlap_sha256", "append_started_at",
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
    test_mode: bool = False,
    _test_crash_at: str | None = None,
) -> PublicationResult:
    """Validate a source snapshot and optionally append an immutable publication.

    ``clock`` is test-only dependency injection and is not exposed by the CLI.
    Dry-runs have no eligibility or version because neither exists before an
    append transaction.  The session cutoff is the real invocation time; the
    eligibility clock is sampled later, after all source validation.
    """
    if not 0 < minimum_latest_coverage <= 1:
        raise PublicationError("Invalid publication coverage policy")
    if _test_crash_at is not None and not test_mode:
        raise PublicationError("Crash injection is test-only")
    if publish and not test_mode:
        require_canonical_paths(source_db_path, factors_path, publication_db_path)
    get_now = clock or (lambda: datetime.now(timezone.utc))
    called_at = utc_datetime(get_now(), label="clock")
    calendar, cutoff_date = latest_completed_session(called_at)
    factors = active_factor_names(factors_path)
    source_date, values, baseline = _snapshot_source(
        source_db_path, factors, calendar, cutoff_date, minimum_latest_coverage, called_at,
    )
    ranking, matrix = ranking_from_values(values, factors)
    factor_hash = sha256_json({"strategy_id": "B16", "factor_names": factors})
    source_hash = sha256_json({
        "factor_group": FACTOR_GROUP, "source_date": source_date,
        "factor_names": factors, "values": values,
    })
    source_path = str(_resolved(source_db_path))
    baseline_symbols = tuple(sorted(baseline.symbols))
    overlap_symbols = tuple(sorted(set(matrix).intersection(baseline_symbols)))
    overlap_hash = sha256_json({"symbols": overlap_symbols})
    coverage_by_factor = {factor: len(overlap_symbols) / baseline.size for factor in factors}
    payload = {
        "schema_version": 3, "strategy_id": "B16", "source_date": source_date,
        "factor_names": list(factors), "factor_set_sha256": factor_hash,
        "values": values, "ranking": ranking, "universe_size": len(matrix),
        "coverage": {"baseline_kind": baseline.kind, "baseline_date": baseline.baseline_date,
                     "baseline_size": baseline.size, "baseline_sha256": baseline.sha256,
                     "baseline_symbols": list(baseline_symbols),
                     "overlap_size": len(overlap_symbols), "overlap_sha256": overlap_hash,
                     "current_overlap": len(overlap_symbols) / len(matrix),
                     "by_factor": coverage_by_factor},
        "source_content_sha256": source_hash,
        "source": {"db_path": source_path, "content_sha256": source_hash},
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
        if _quarantine_state(lock_descriptor) is not None:
            _recover_locked(publication_db_path, lock_descriptor)
        require_store_not_quarantined(lock_descriptor)
        con = initialize_store(publication_db_path)
        _require_descriptor_path(_lock_path(publication_db_path), lock_descriptor)
        _require_connection_path(con)
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
            if append_started < called_at:
                raise PublicationError("Publication clock moved backwards before append")
            eligible = append_started + timedelta(seconds=ELIGIBILITY_DELAY_SECONDS)
            append_text = timestamp_text(append_started)
            eligible_text = timestamp_text(eligible)
            latest_times = con.execute(
                "SELECT MAX(append_started_at),MAX(eligible_at) FROM signal_publications "
                "WHERE strategy_id='B16'"
            ).fetchone()
            if ((latest_times[0] is not None and append_text <= str(latest_times[0]))
                    or (latest_times[1] is not None and eligible_text <= str(latest_times[1]))):
                raise PublicationError("Publication wall clock is not strictly monotonic")
            version = int(con.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM signal_publications "
                "WHERE strategy_id='B16' AND source_date=?", (source_date,),
            ).fetchone()[0])
            values_by_name: dict[str, Any] = {
                "strategy_id": "B16", "source_date": source_date, "eligible_at": eligible_text,
                "version": version, "factor_names_json": canonical_json(list(factors)),
                "factor_set_sha256": factor_hash, "payload_json": payload_text,
                "payload_sha256": payload_hash,
                "source_db_path": source_path,
                "source_content_sha256": source_hash, "calendar_name": CALENDAR_NAME,
                "calendar_version": EXPECTED_CALENDAR_VERSION, "universe_size": len(matrix),
                "baseline_kind": baseline.kind, "baseline_date": baseline.baseline_date,
                "baseline_size": baseline.size, "baseline_sha256": baseline.sha256,
                "baseline_symbols_json": canonical_json(list(baseline_symbols)),
                "overlap_size": len(overlap_symbols), "overlap_sha256": overlap_hash,
                "append_started_at": append_text,
            }
            record_hash = sha256_json(publication_record_material(values_by_name))
            set_store_quarantine(lock_descriptor, {
                "state": "append_unresolved", "eligible_at": eligible_text,
                "source_date": source_date, "version": version,
                "record_sha256": record_hash, "payload_sha256": payload_hash,
            })
            quarantine_set = True
            if _test_crash_at == "after_quarantine":
                raise _SimulatedPublicationCrash()
            cursor = con.execute(
                """INSERT INTO signal_publications(
                    strategy_id,source_date,eligible_at,version,factor_names_json,
                    factor_set_sha256,payload_json,payload_sha256,source_db_path,
                    source_content_sha256,calendar_name,calendar_version,universe_size,
                    baseline_kind,baseline_date,baseline_size,baseline_sha256,
                    baseline_symbols_json,overlap_size,overlap_sha256,append_started_at,record_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*publication_record_material(values_by_name).values(), record_hash),
            )
            if cursor.lastrowid is None:
                raise PublicationError("Publication append did not return an id")
            publication_id = cursor.lastrowid
            if _test_crash_at == "after_insert":
                raise _SimulatedPublicationCrash()
            commit_attempted = True
            _require_descriptor_path(_lock_path(publication_db_path), lock_descriptor)
            _require_connection_path(con)
            con.commit()
            if _test_crash_at == "after_commit":
                raise _SimulatedPublicationCrash()
            committed_returned_at = utc_datetime(get_now(), label="clock")
            if committed_returned_at < append_started:
                raise PublicationError("Publication clock moved backwards after commit")
            if committed_returned_at >= eligible:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "INSERT INTO signal_publication_revocations(publication_id,revoked_at,reason) "
                    "VALUES(?,?,?)",
                    (publication_id, timestamp_text(committed_returned_at),
                     "append commit did not complete before eligible_at"),
                )
                _require_descriptor_path(_lock_path(publication_db_path), lock_descriptor)
                _require_connection_path(con)
                con.commit()
                _require_descriptor_path(_lock_path(publication_db_path), lock_descriptor)
                _require_connection_path(con)
                set_store_quarantine(lock_descriptor, None)
                quarantine_set = False
                raise PublicationError("Publication commit missed eligibility and was revoked")
            _require_descriptor_path(_lock_path(publication_db_path), lock_descriptor)
            _require_connection_path(con)
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
