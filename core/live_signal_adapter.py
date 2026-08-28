"""Read-only adapter from persisted B16 GP factors to a live ranking batch."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path("/home/gexin/quant-trading/data/trading.db")
DEFAULT_FACTORS_PATH = Path("/home/gexin/quant-trading/factors/mined_alphas_per_account.json")
FACTOR_GROUP = "gp_B16"


class SignalAdapterError(RuntimeError):
    """Raised when persisted inputs cannot safely produce a B16 signal batch."""


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


def _sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_utc(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise SignalAdapterError("as_of must be timezone-aware")
    return result.astimezone(timezone.utc)


def _latest_completed_nyse_session(now: datetime) -> tuple[Any, date]:
    """Return the XNYS calendar and latest session whose official close passed."""
    try:
        import exchange_calendars as xcals
        import pandas as pd

        calendar = xcals.get_calendar("XNYS")
        candidate = calendar.date_to_session(pd.Timestamp(now.date()), direction="previous")
        while calendar.session_close(candidate).to_pydatetime() > now:
            candidate = calendar.previous_session(candidate)
        return calendar, candidate.date()
    except Exception as exc:
        # Daily live factors must never fall back to weekday/time arithmetic: that
        # would silently mishandle holidays, DST and special closes.
        raise SignalAdapterError("Unable to determine completed NYSE calendar session") from exc


def _parse_session_date(value: str, calendar: Any, *, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        import pandas as pd

        if not calendar.is_session(pd.Timestamp(parsed)):
            raise SignalAdapterError(f"{label} B16 source date is not an NYSE session")
        return parsed
    except SignalAdapterError:
        raise
    except Exception as exc:
        raise SignalAdapterError(f"Invalid {label.lower()} B16 source date") from exc


def _active_factor_names(path: str | Path) -> tuple[str, ...]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = document["B16"]
        active = [entry for entry in entries
                  if entry.get("expression") and entry.get("active", True) is not False]
        names = tuple(str(entry["name"]).strip() for entry in active)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SignalAdapterError("Unable to read active B16 factor set") from exc
    if not names or any(not name for name in names) or len(set(names)) != len(names):
        raise SignalAdapterError("Active B16 factor set is empty or invalid")
    # A factor set has no semantic ordering; canonicalize it so harmless JSON
    # reordering cannot rotate hashes or batches.
    return tuple(sorted(names))


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _average_percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Match pandas Series.rank(method='average', pct=True), ascending=True."""
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    ranks: dict[str, float] = {}
    index = 0
    while index < count:
        end = index + 1
        while end < count and ordered[end][1] == ordered[index][1]:
            end += 1
        average_rank = ((index + 1) + end) / 2.0
        percentile = average_rank / count
        for position in range(index, end):
            ranks[ordered[position][0]] = percentile
        index = end
    return ranks


def load_b16_signal_batch(
    db_path: str | Path = DEFAULT_DB_PATH,
    factors_path: str | Path = DEFAULT_FACTORS_PATH,
    *,
    strategy_id: str = "B16",
    as_of: datetime | None = None,
    max_age_days: int = 4,
    minimum_latest_coverage: float = 0.90,
    sell_tail_size: int = 4,
) -> SignalBatch:
    """Load the latest valid gp_B16 cross-section completed by ``as_of``.

    The database is opened through SQLite's URI ``mode=ro`` and query-only mode.
    The point-in-time cutoff is the most recent officially closed NYSE session.
    No paper account object, state, or QuantSystem import participates in ranking.
    """
    if strategy_id != "B16":
        raise SignalAdapterError("Only strategy B16 is supported")
    if max_age_days < 0 or not 0 < minimum_latest_coverage <= 1:
        raise SignalAdapterError("Invalid freshness or coverage policy")
    if isinstance(sell_tail_size, bool) or sell_tail_size < 1:
        raise SignalAdapterError("sell_tail_size must be positive")

    factor_names = _active_factor_names(factors_path)
    now = _as_utc(as_of)
    calendar, cutoff_date = _latest_completed_nyse_session(now)
    cutoff = cutoff_date.isoformat()
    placeholders = ",".join("?" for _ in factor_names)
    try:
        with _readonly_connection(db_path) as con:
            latest_row = con.execute(
                "SELECT MAX(date) FROM factor_values WHERE factor_group=? AND date<=?",
                (FACTOR_GROUP, cutoff),
            ).fetchone()
            source_date = str(latest_row[0]) if latest_row and latest_row[0] else ""
            if not source_date:
                future_exists = con.execute(
                    "SELECT EXISTS(SELECT 1 FROM factor_values "
                    "WHERE factor_group=? AND date>?)",
                    (FACTOR_GROUP, cutoff),
                ).fetchone()[0]
                if future_exists:
                    raise SignalAdapterError("B16 source date is in the future")
                raise SignalAdapterError("No gp_B16 factor values")
            parsed_date = _parse_session_date(source_date, calendar, label="Selected")
            age = (cutoff_date - parsed_date).days
            if age < 0:
                raise SignalAdapterError("B16 source date is in the future")
            if age > max_age_days:
                raise SignalAdapterError("B16 source date is stale")
            rows = con.execute(
                "SELECT ticker,factor_name,value FROM factor_values "
                "WHERE factor_group=? AND date=? ORDER BY ticker,factor_name",
                (FACTOR_GROUP, source_date),
            ).fetchall()
            previous = con.execute(
                "SELECT MAX(date) FROM factor_values WHERE factor_group=? AND date<?",
                (FACTOR_GROUP, source_date),
            ).fetchone()[0]
            previous_counts: list[int] = []
            if previous:
                _parse_session_date(str(previous), calendar, label="Previous")
                previous_counts = [int(row[0]) for row in con.execute(
                    f"SELECT COUNT(DISTINCT ticker) FROM factor_values "
                    f"WHERE factor_group=? AND date=? AND factor_name IN ({placeholders}) "
                    "GROUP BY factor_name",
                    (FACTOR_GROUP, previous, *factor_names),
                )]
    except SignalAdapterError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise SignalAdapterError("Unable to read B16 factor database") from exc

    observed_factors = {str(row["factor_name"]) for row in rows}
    if observed_factors != set(factor_names):
        raise SignalAdapterError("Latest B16 factor set does not match active factor set")

    matrix: dict[str, dict[str, float]] = {}
    for row in rows:
        symbol = str(row["ticker"]).strip()
        factor = str(row["factor_name"])
        try:
            value = float(row["value"])
        except (TypeError, ValueError) as exc:
            raise SignalAdapterError("B16 factor values must be finite") from exc
        if not symbol or not math.isfinite(value):
            raise SignalAdapterError("B16 factor values must be finite")
        matrix.setdefault(symbol, {})[factor] = value
    if not matrix or any(set(values) != set(factor_names) for values in matrix.values()):
        raise SignalAdapterError("Latest B16 cross-section is not complete")
    if previous_counts:
        prior_size = max(previous_counts)
        if prior_size and len(matrix) / prior_size < minimum_latest_coverage:
            raise SignalAdapterError("Latest B16 cross-section coverage is partial")

    factor_ranks = {
        factor: _average_percentile_ranks({symbol: values[factor]
                                           for symbol, values in matrix.items()})
        for factor in factor_names
    }
    scored = [
        RankedSignal(symbol, sum(factor_ranks[factor][symbol] for factor in factor_names)
                     / len(factor_names))
        for symbol in matrix
    ]
    scored.sort(key=lambda row: (-row.score, row.symbol))
    if not scored:
        raise SignalAdapterError("Latest B16 ranking is empty")

    tail_count = min(int(sell_tail_size), max(1, len(scored) // 2))
    ranking = tuple(scored)
    sell_tail = tuple(row.symbol for row in ranking[-tail_count:])
    buy_candidates = tuple(row.symbol for row in ranking[:-tail_count])
    factor_set_hash = _sha256({"strategy_id": strategy_id, "factor_names": factor_names})
    batch_id = _sha256({
        "strategy_id": strategy_id,
        "source_date": source_date,
        "factor_set_hash": factor_set_hash,
        "ranking": [(row.symbol, format(row.score, ".17g")) for row in ranking],
    })
    return SignalBatch(strategy_id, source_date, factor_names, factor_set_hash, batch_id,
                       ranking, buy_candidates, sell_tail)
