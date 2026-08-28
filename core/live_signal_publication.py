"""Append-only point-in-time publications for B16 live signals.

The mutable research ``factor_values`` table is an input to an explicit publish
step only.  Live execution reads this module-owned store and never that table.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

EXPECTED_CALENDAR_VERSION = "4.13.2"
CALENDAR_NAME = "XNYS"
FACTOR_GROUP = "gp_B16"
SCHEMA_VERSION = 1
DEFAULT_SOURCE_DB_PATH = Path("/home/gexin/quant-trading/data/trading.db")
DEFAULT_FACTORS_PATH = Path("/home/gexin/quant-trading/factors/mined_alphas_per_account.json")
DEFAULT_PUBLICATION_DB_PATH = Path(__file__).resolve().parents[1] / "data/live_signal_publications.db"


class PublicationError(RuntimeError):
    """Raised when a source cannot be proven safe to publish."""


@dataclass(frozen=True)
class PublicationResult:
    strategy_id: str
    source_date: str
    published_at: str
    version: int
    payload_sha256: str
    source_content_sha256: str
    persisted: bool


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
        # Prove the named calendar has schedule coverage around the live horizon.
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
    observed = {factor for row in matrix.values() for factor in row}
    if observed != expected:
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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_publications(
    publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL CHECK(strategy_id='B16'),
    source_date TEXT NOT NULL,
    published_at TEXT NOT NULL,
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
    prior_universe_size INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(strategy_id, source_date, version),
    UNIQUE(strategy_id, source_date, factor_set_sha256, payload_sha256)
);
CREATE INDEX IF NOT EXISTS idx_signal_publications_pit
ON signal_publications(strategy_id, published_at, source_date, publication_id);
CREATE TRIGGER IF NOT EXISTS signal_publications_no_update
BEFORE UPDATE ON signal_publications BEGIN
    SELECT RAISE(ABORT, 'signal publications are immutable');
END;
CREATE TRIGGER IF NOT EXISTS signal_publications_no_delete
BEFORE DELETE ON signal_publications BEGIN
    SELECT RAISE(ABORT, 'signal publications are immutable');
END;
"""


def initialize_store(path: str | Path) -> sqlite3.Connection:
    destination = Path(path).expanduser().resolve()
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(destination.parent, 0o700)
    con = sqlite3.connect(destination, timeout=30, isolation_level=None)
    try:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise PublicationError(f"Unsupported publication schema version {version}")
        con.executescript(_SCHEMA)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        os.chmod(destination, 0o600)
        return con
    except Exception:
        con.close()
        raise


def validate_store_schema(con: sqlite3.Connection) -> None:
    try:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_publications'"
        ).fetchone()
        triggers = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='signal_publications'"
            )
        }
    except sqlite3.Error as exc:
        raise PublicationError("Unable to inspect publication schema") from exc
    required_triggers = {"signal_publications_no_update", "signal_publications_no_delete"}
    if version != SCHEMA_VERSION or not table or not required_triggers.issubset(triggers):
        raise PublicationError("Unsupported or missing publication schema integrity controls")


def _snapshot_source(
    source_db_path: str | Path,
    factor_names: tuple[str, ...],
    calendar: Any,
    cutoff_date: date,
    minimum_latest_coverage: float,
) -> tuple[str, list[list[Any]], int | None]:
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
            previous = con.execute(
                "SELECT MAX(date) FROM factor_values WHERE factor_group=? AND date<?",
                (FACTOR_GROUP, source_date),
            ).fetchone()[0]
            prior_size = None
            if previous:
                parse_session(str(previous), calendar, label="Previous")
                placeholders = ",".join("?" for _ in factor_names)
                counts = [int(row[0]) for row in con.execute(
                    f"SELECT COUNT(DISTINCT ticker) FROM factor_values WHERE factor_group=? "
                    f"AND date=? AND factor_name IN ({placeholders}) GROUP BY factor_name",
                    (FACTOR_GROUP, previous, *factor_names),
                )]
                prior_size = max(counts) if counts else None
            con.commit()
    except PublicationError:
        raise
    except sqlite3.Error as exc:
        raise PublicationError("Unable to read a consistent B16 source snapshot") from exc

    normalized = [[str(row["ticker"]).strip(), str(row["factor_name"]), row["value"]]
                  for row in rows]
    _, matrix = ranking_from_values(normalized, factor_names)
    if prior_size and len(matrix) / prior_size < minimum_latest_coverage:
        raise PublicationError("Latest B16 cross-section coverage is partial")
    return source_date, normalized, prior_size


def publish_b16_signal(
    source_db_path: str | Path = DEFAULT_SOURCE_DB_PATH,
    factors_path: str | Path = DEFAULT_FACTORS_PATH,
    publication_db_path: str | Path = DEFAULT_PUBLICATION_DB_PATH,
    *,
    published_at: datetime | None = None,
    publish: bool = False,
    minimum_latest_coverage: float = 0.90,
) -> PublicationResult:
    """Validate a read-only source snapshot and optionally append its publication.

    ``publish=False`` is deliberately the default.  ``published_at`` exists for
    deterministic tests/programmatic replay; the production CLI never exposes it.
    """
    if not 0 < minimum_latest_coverage <= 1:
        raise PublicationError("Invalid publication coverage policy")
    now = utc_datetime(published_at, label="published_at")
    calendar, cutoff_date = latest_completed_session(now)
    factors = active_factor_names(factors_path)
    source_date, values, prior_size = _snapshot_source(
        source_db_path, factors, calendar, cutoff_date, minimum_latest_coverage,
    )
    ranking, matrix = ranking_from_values(values, factors)
    factor_hash = sha256_json({"strategy_id": "B16", "factor_names": factors})
    source_hash = sha256_json({
        "factor_group": FACTOR_GROUP, "source_date": source_date,
        "factor_names": factors, "values": values,
    })
    payload = {
        "schema_version": 1, "strategy_id": "B16", "source_date": source_date,
        "factor_names": list(factors), "factor_set_sha256": factor_hash,
        "values": values, "ranking": ranking, "universe_size": len(matrix),
        "prior_universe_size": prior_size, "source_content_sha256": source_hash,
        "calendar": {"name": CALENDAR_NAME, "version": EXPECTED_CALENDAR_VERSION},
    }
    payload_text = canonical_json(payload)
    payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    published_text = timestamp_text(now)
    preview = PublicationResult(
        # No version exists until the append transaction assigns one.  Reporting
        # zero keeps dry-run output honest when a store already contains versions.
        "B16", source_date, published_text, 0, payload_hash, source_hash, False,
    )
    if not publish:
        return preview

    con = initialize_store(publication_db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT version,published_at FROM signal_publications WHERE strategy_id='B16' "
            "AND source_date=? AND factor_set_sha256=? AND payload_sha256=?",
            (source_date, factor_hash, payload_hash),
        ).fetchone()
        if existing:
            con.commit()
            return PublicationResult(
                "B16", source_date, str(existing[1]), int(existing[0]), payload_hash,
                source_hash, True,
            )
        version = int(con.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM signal_publications "
            "WHERE strategy_id='B16' AND source_date=?", (source_date,),
        ).fetchone()[0])
        con.execute(
            """INSERT INTO signal_publications(
                strategy_id,source_date,published_at,version,factor_names_json,
                factor_set_sha256,payload_json,payload_sha256,source_db_path,
                source_content_sha256,calendar_name,calendar_version,universe_size,
                prior_universe_size,created_at
            ) VALUES('B16',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source_date, published_text, version, canonical_json(list(factors)), factor_hash,
             payload_text, payload_hash, str(Path(source_db_path).expanduser().resolve()),
             source_hash, CALENDAR_NAME, EXPECTED_CALENDAR_VERSION, len(matrix), prior_size,
             timestamp_text(datetime.now(timezone.utc))),
        )
        con.commit()
        return PublicationResult(
            "B16", source_date, published_text, version, payload_hash, source_hash, True,
        )
    except sqlite3.Error as exc:
        con.rollback()
        raise PublicationError("Unable to append immutable B16 publication") from exc
    finally:
        con.close()
