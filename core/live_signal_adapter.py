"""Point-in-time adapter from immutable B16 publications to a live ranking batch."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.live_signal_publication import (
    CALENDAR_NAME,
    DEFAULT_FACTORS_PATH,
    DEFAULT_PUBLICATION_DB_PATH,
    EXPECTED_CALENDAR_VERSION,
    FACTOR_GROUP,
    PublicationError,
    active_factor_names,
    canonical_json,
    latest_completed_session,
    parse_session,
    ranking_from_values,
    sha256_json,
    timestamp_text,
    validate_store_schema,
)

DEFAULT_DB_PATH = DEFAULT_PUBLICATION_DB_PATH


class SignalAdapterError(RuntimeError):
    """Raised when no verifiable point-in-time B16 publication is available."""


@dataclass(frozen=True)
class RankedSignal:
    symbol: str
    score: float


@dataclass(frozen=True)
class SignalBatch:
    strategy_id: str
    source_date: str
    factor_names: tuple[str, ...]
    factor_set_hash: str
    batch_id: str
    ranking: tuple[RankedSignal, ...]
    buy_candidates: tuple[str, ...]
    sell_tail: tuple[str, ...]
    publication_version: int = 0
    published_at: str = ""
    payload_sha256: str = ""


def _as_utc(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise SignalAdapterError("as_of must be timezone-aware")
    return result.astimezone(timezone.utc)


def _publication_connection(path: str | Path) -> sqlite3.Connection:
    con: sqlite3.Connection | None = None
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        con = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        validate_store_schema(con)
        return con
    except PublicationError as exc:
        if con is not None:
            con.close()
        raise SignalAdapterError(str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        if con is not None:
            con.close()
        raise SignalAdapterError("No verifiable B16 publication store") from exc


def _integrity_error(message: str = "B16 publication integrity check failed") -> SignalAdapterError:
    return SignalAdapterError(message)


def _validate_publication(
    row: sqlite3.Row,
    expected_factors: tuple[str, ...],
    calendar: Any,
    cutoff_date: Any,
    as_of: datetime,
    minimum_latest_coverage: float,
    max_age_days: int,
) -> tuple[dict[str, Any], list[list[str]]]:
    try:
        payload_text = str(row["payload_json"])
        payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        if payload_hash != row["payload_sha256"]:
            raise _integrity_error()
        payload = json.loads(payload_text)
        if canonical_json(payload) != payload_text:
            raise _integrity_error()
        published_at = datetime.fromisoformat(str(row["published_at"]))
        if (published_at.tzinfo is None
                or timestamp_text(published_at) != str(row["published_at"])
                or published_at.astimezone(timezone.utc) > as_of):
            raise _integrity_error("B16 publication timestamp is invalid")
        source_date = str(row["source_date"])
        parsed_date = parse_session(source_date, calendar, label="Published")
        if parsed_date > cutoff_date:
            raise _integrity_error("B16 publication source session is not completed")
        age = (cutoff_date - parsed_date).days
        if age < 0 or age > max_age_days:
            raise SignalAdapterError("B16 publication source date is stale")

        factor_names = tuple(json.loads(str(row["factor_names_json"])))
        factor_hash = sha256_json({"strategy_id": "B16", "factor_names": factor_names})
        if factor_names != expected_factors:
            raise SignalAdapterError("B16 publication factor set does not match active factor set")
        if factor_hash != row["factor_set_sha256"]:
            raise _integrity_error()
        if (payload.get("schema_version") != 1 or payload.get("strategy_id") != "B16"
                or payload.get("source_date") != source_date
                or tuple(payload.get("factor_names", ())) != factor_names
                or payload.get("factor_set_sha256") != factor_hash):
            raise _integrity_error()
        if (row["calendar_name"] != CALENDAR_NAME
                or row["calendar_version"] != EXPECTED_CALENDAR_VERSION
                or payload.get("calendar") != {
                    "name": CALENDAR_NAME, "version": EXPECTED_CALENDAR_VERSION,
                }):
            raise _integrity_error("B16 publication calendar metadata is invalid")

        values = payload.get("values")
        if not isinstance(values, list):
            raise _integrity_error()
        ranking, matrix = ranking_from_values(values, factor_names)
        source_hash = sha256_json({
            "factor_group": FACTOR_GROUP, "source_date": source_date,
            "factor_names": factor_names, "values": values,
        })
        if (source_hash != row["source_content_sha256"]
                or payload.get("source_content_sha256") != source_hash
                or payload.get("ranking") != ranking
                or payload.get("universe_size") != len(matrix)
                or int(row["universe_size"]) != len(matrix)):
            raise _integrity_error()
        prior = payload.get("prior_universe_size")
        stored_prior = row["prior_universe_size"]
        if prior != stored_prior:
            raise _integrity_error()
        if prior is not None:
            if isinstance(prior, bool) or not isinstance(prior, int) or prior <= 0:
                raise _integrity_error()
            if len(matrix) / prior < minimum_latest_coverage:
                raise SignalAdapterError("B16 publication cross-section coverage is partial")
        return payload, ranking
    except SignalAdapterError:
        raise
    except PublicationError as exc:
        raise SignalAdapterError(str(exc)) from exc
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _integrity_error() from exc


def load_b16_signal_batch(
    db_path: str | Path = DEFAULT_PUBLICATION_DB_PATH,
    factors_path: str | Path = DEFAULT_FACTORS_PATH,
    *,
    strategy_id: str = "B16",
    as_of: datetime | None = None,
    max_age_days: int = 4,
    minimum_latest_coverage: float = 0.90,
    sell_tail_size: int = 4,
) -> SignalBatch:
    """Load the newest publication proven available and complete at ``as_of``.

    ``db_path`` is the module-owned publication store, not the mutable research
    factor database.  Absence of a qualifying immutable publication fails closed.
    """
    if strategy_id != "B16":
        raise SignalAdapterError("Only strategy B16 is supported")
    if max_age_days < 0 or not 0 < minimum_latest_coverage <= 1:
        raise SignalAdapterError("Invalid freshness or coverage policy")
    if isinstance(sell_tail_size, bool) or sell_tail_size < 1:
        raise SignalAdapterError("sell_tail_size must be positive")
    now = _as_utc(as_of)
    try:
        expected_factors = active_factor_names(factors_path)
        calendar, cutoff_date = latest_completed_session(now)
    except PublicationError as exc:
        raise SignalAdapterError(str(exc)) from exc

    with _publication_connection(db_path) as con:
        try:
            row = con.execute(
                "SELECT * FROM signal_publications WHERE strategy_id=? "
                "AND published_at<=? AND source_date<=? "
                "ORDER BY published_at DESC,publication_id DESC LIMIT 1",
                (strategy_id, timestamp_text(now), cutoff_date.isoformat()),
            ).fetchone()
        except sqlite3.Error as exc:
            raise SignalAdapterError("Unable to read B16 publication store") from exc
    if row is None:
        raise SignalAdapterError("No eligible B16 publication exists for as_of")

    _, ranking_payload = _validate_publication(
        row, expected_factors, calendar, cutoff_date, now,
        minimum_latest_coverage, max_age_days,
    )
    ranking = tuple(RankedSignal(str(item[0]), float(item[1])) for item in ranking_payload)
    if not ranking or any(not item.symbol or not math.isfinite(item.score) for item in ranking):
        raise _integrity_error()
    tail_count = min(int(sell_tail_size), max(1, len(ranking) // 2))
    sell_tail = tuple(item.symbol for item in ranking[-tail_count:])
    buy_candidates = tuple(item.symbol for item in ranking[:-tail_count])
    batch_id = sha256_json({
        "strategy_id": strategy_id,
        "source_date": row["source_date"],
        "publication_version": int(row["version"]),
        "payload_sha256": row["payload_sha256"],
    })
    return SignalBatch(
        strategy_id, str(row["source_date"]), expected_factors,
        str(row["factor_set_sha256"]), batch_id, ranking, buy_candidates, sell_tail,
        int(row["version"]), str(row["published_at"]), str(row["payload_sha256"]),
    )
