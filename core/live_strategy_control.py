"""Independent USD 10k live-strategy ledger and fail-closed control plane.

This database contains strategy state only. Broker account identifiers, passwords,
tokens and raw order/deal references are intentionally excluded. External broker
positions are never imported as strategy-owned positions.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.live_logging import redact as _redact

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "live_strategy.db"
ARCHIVE_DIR = Path(__file__).resolve().parents[1] / "data" / "live_strategy_archives"
INITIAL_CAPITAL = 10_000.0
EXPOSURE_CAP = 10_000.0
LOSS_FLOOR = 7_500.0
SQLITE_BUSY_TIMEOUT_SECONDS = 20.0

DEFAULT_CONFIG: dict[str, Any] = {
    "strategy_id": "B16",
    "top_n": 6,
    "position_target_pct": 0.1466,
    "gross_target_pct": 0.88,
    "stop_loss_pct": 0.08,
    "stop_cooldown_hours": 72,
    "min_hold_days": 0,
    "hold_band_mult": 4,
    "rebalance_hours": 12,
    "max_order_notional": 2500.0,
    "max_daily_order_notional": 5000.0,
    "max_limit_deviation_pct": 0.02,
    "max_quote_age_seconds": 120,
}

EDITABLE_FIELDS = frozenset(DEFAULT_CONFIG) - {"strategy_id"}
_INTENT_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DASHBOARD_FREEZE_CODES = frozenset({
    "manual_freeze", "operator_requested_freeze", "cleanup_requested",
})
_OPERATOR_ACTORS = frozenset({"dashboard", "operator", "manual_api", "operator_cleanup"})
EXECUTION_HOLD_SCOPES = frozenset({"SYSTEM", "SYMBOL", "INTENT"})
_INTERNAL_FREEZE_CODES = frozenset({
    "auto_broker_outcome_unknown",
    "auto_intent_broker_outcome_unknown",
    "auto_intent_broker_proof_mismatch",
    "auto_intent_payload_conflict",
    "auto_post_broker_reconciliation_failed",
    "auto_unclassified_dispatch_failure",
    "cleaned_no_valid_strategy",
    "cleanup_requested",
    "config_changed_requires_review",
    "control_generation_changed_requires_sync",
    "five_minute_reconciliation_failed",
    "manual_freeze",
    "not_provisioned",
    "operator_requested_freeze",
    "owned_price_missing",
    "reconciliation_fill_conflict",
    "reconciliation_quantity_mismatch",
    "reconciliation_snapshot_deal_conflict",
    "reconciliation_snapshot_numeric_conflict",
    "reconciliation_snapshot_order_conflict",
    "reconciliation_snapshot_position_conflict",
    "runtime_identity_changed_requires_sync",
    "sanitized_freeze_reason",
    "strategy_equity_at_or_below_7500",
    "strategy_exposure_above_10000",
})
_LEGACY_AUTOMATIC_FREEZE_CODES = _INTERNAL_FREEZE_CODES - frozenset({
    "cleaned_no_valid_strategy", "cleanup_requested", "manual_freeze",
    "not_provisioned", "operator_requested_freeze", "sanitized_freeze_reason",
})
_WATCHDOG_PROBLEM_CODES = frozenset({
    "ACCOUNT_ISOLATION_SYNC_PROOF_MISMATCH",
    "AUTO_INTENT_DISPATCH_STALE",
    "AUTO_INTENT_OUTCOME_UNKNOWN",
    "BROKER_OUTCOME_REQUIRES_RECONCILIATION",
    "EXPOSURE_CAP_BREACH",
    "FIVE_MINUTE_SYNC_STALE",
    "LOSS_FLOOR_BREACH",
    "MODULE_ORDER_STUCK_OVER_15_MIN",
    "MOOMOO_HISTORY_INCOMPLETE",
    "MOOMOO_UNAVAILABLE_WHILE_ACTIVE",
})
_WATCHDOG_STALE_CODES = frozenset({
    "AUTO_INTENT_STALE:ACKED", "AUTO_INTENT_STALE:PARTIAL", "AUTO_INTENT_STALE:RESERVED",
})
AUTO_INTENT_STATUSES = frozenset({
    "PLANNED", "RESERVED", "DISPATCHING", "ACKED", "PARTIAL",
    "FILLED", "CANCELLED", "FAILED", "UNKNOWN",
})
AUTO_INTENT_TERMINAL = frozenset({"FILLED", "CANCELLED", "FAILED"})
AUTO_INTENT_UNRESOLVED = frozenset({"PLANNED", "RESERVED", "DISPATCHING", "ACKED", "PARTIAL", "UNKNOWN"})


class ControlRejected(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ControlRejected(f"Invalid {name}") from exc
    if not math.isfinite(result):
        raise ControlRejected(f"Invalid {name}")
    return result


def _valid_watchdog_reason(raw: str) -> bool:
    if not raw.startswith("health_watchdog:"):
        return False
    problems = raw.removeprefix("health_watchdog:").split(",")
    if not problems or any(not problem for problem in problems):
        return False
    for problem in problems:
        if problem in _WATCHDOG_PROBLEM_CODES or problem in _WATCHDOG_STALE_CODES:
            continue
        if problem.startswith("SYSTEM_FROZEN:"):
            frozen_code = problem.removeprefix("SYSTEM_FROZEN:")
            if frozen_code in _INTERNAL_FREEZE_CODES:
                continue
        return False
    return True


def _safe_freeze_reason(reason: Any, source: str = "") -> tuple[str, bool]:
    """Return a bounded state-machine code, never caller-controlled prose."""
    raw = str(reason or "").strip()
    if not raw:
        raw = "manual_freeze"
    if str(source) == "dashboard" and raw not in _DASHBOARD_FREEZE_CODES:
        return "operator_requested_freeze", raw != "operator_requested_freeze"
    if raw.startswith("health_watchdog:"):
        if str(source) in {"", "health_watchdog"} and _valid_watchdog_reason(raw):
            return raw, False
        return "sanitized_freeze_reason", True
    if raw in _INTERNAL_FREEZE_CODES:
        return raw, False
    return "sanitized_freeze_reason", True


def freeze_reason_code(reason: Any, source: str = "") -> str:
    """Public read-boundary helper for safe freeze-state serialization."""
    return _safe_freeze_reason(reason, source)[0]


@dataclass(frozen=True)
class RiskSnapshot:
    lifecycle: str
    frozen: bool
    freeze_reason: str | None
    initial_capital: float
    exposure_cap: float
    loss_floor: float
    allocated_cash: float
    owned_market_value: float
    strategy_equity: float
    realized_pnl: float
    unrealized_pnl: float
    reserved_buy_notional: float
    config_version: int
    strategy_id: str | None
    last_sync_at: str | None
    required_sync_after: str | None


class LiveStrategyStore:
    def __init__(self, path: str | Path = DB_PATH, archive_dir: str | Path = ARCHIVE_DIR,
                 *, read_only: bool = False):
        self.path = Path(path)
        self.archive_dir = Path(archive_dir)
        self.read_only = bool(read_only)
        if not self.read_only:
            self._initialize()

    @contextmanager
    def connect(self):
        if self.read_only:
            resolved = self.path.expanduser().resolve(strict=True)
            con = sqlite3.connect(
                f"{resolved.as_uri()}?mode=ro", uri=True,
                timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            )
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            try:
                yield con
            finally:
                con.close()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        con.row_factory = sqlite3.Row
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        con.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _enable_wal_mode(self) -> None:
        """Enable WAL outside migration transactions, retrying SQLite's immediate BUSY."""
        deadline = time.monotonic() + SQLITE_BUSY_TIMEOUT_SECONDS
        delay = 0.001
        while True:
            try:
                with sqlite3.connect(
                    str(self.path), timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
                ) as con:
                    con.execute(
                        f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}"
                    )
                    if str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                        return
                    mode = str(con.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
                    if mode != "wal":
                        raise sqlite3.OperationalError(
                            f"SQLite refused WAL journal mode (reported {mode!r})"
                        )
                    return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise sqlite3.OperationalError(
                        "Timed out enabling WAL journal mode before schema initialization"
                    ) from exc
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.05)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._enable_wal_mode()
        with self.connect() as con:
            # executescript commits a transaction opened before it, so acquire
            # the migration lock inside the script itself.
            con.executescript("""
                BEGIN EXCLUSIVE;
                CREATE TABLE IF NOT EXISTS strategy_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('UNCONFIGURED','FROZEN','ACTIVE','CLEANED')),
                    freeze_latched INTEGER NOT NULL CHECK(freeze_latched IN (0,1)),
                    freeze_reason TEXT,
                    strategy_id TEXT,
                    initial_capital REAL NOT NULL CHECK(initial_capital=10000.0),
                    exposure_cap REAL NOT NULL CHECK(exposure_cap=10000.0),
                    loss_floor REAL NOT NULL CHECK(loss_floor=7500.0),
                    allocated_cash REAL NOT NULL,
                    owned_market_value REAL NOT NULL,
                    strategy_equity REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    reserved_buy_notional REAL NOT NULL,
                    config_version INTEGER NOT NULL,
                    last_sync_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    required_sync_after TEXT
                );
                CREATE TABLE IF NOT EXISTS strategy_config (
                    version INTEGER PRIMARY KEY,
                    active INTEGER NOT NULL CHECK(active IN (0,1)),
                    config_json TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    changed_by TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_config
                    ON strategy_config(active) WHERE active=1;
                CREATE TABLE IF NOT EXISTS owned_positions (
                    symbol TEXT PRIMARY KEY,
                    quantity REAL NOT NULL CHECK(quantity>=0),
                    average_cost REAL NOT NULL CHECK(average_cost>=0),
                    market_price REAL NOT NULL CHECK(market_price>=0),
                    market_value REAL NOT NULL CHECK(market_value>=0),
                    realized_pnl REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS applied_fills (
                    fill_hash TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    quantity REAL NOT NULL CHECK(quantity>0),
                    price REAL NOT NULL CHECK(price>=0),
                    fee REAL NOT NULL CHECK(fee>=0),
                    applied_at TEXT NOT NULL,
                    fee_is_stable INTEGER NOT NULL DEFAULT 0 CHECK(fee_is_stable IN (0,1)),
                    order_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS order_fee_accounts (
                    order_hash TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    cumulative_fee REAL NOT NULL CHECK(cumulative_fee>=0),
                    finalized INTEGER NOT NULL CHECK(finalized IN (0,1)),
                    revision INTEGER NOT NULL CHECK(revision>=0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_fee_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    adjustment_hash TEXT NOT NULL UNIQUE,
                    order_hash TEXT NOT NULL,
                    previous_total REAL NOT NULL CHECK(previous_total>=0),
                    new_total REAL NOT NULL CHECK(new_total>=0),
                    fill_fee_credit REAL NOT NULL CHECK(fill_fee_credit>=0),
                    delta REAL NOT NULL,
                    applied_at TEXT NOT NULL,
                    FOREIGN KEY(order_hash) REFERENCES order_fee_accounts(order_hash)
                );
                CREATE TABLE IF NOT EXISTS external_symbol_denylist (
                    symbol TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_sync_proof (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    fingerprint TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    control_generation INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS control_runtime (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    generation INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    changed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manual_symbol_conflicts (
                    symbol TEXT PRIMARY KEY,
                    detected_at TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    config_version INTEGER
                );
                CREATE TABLE IF NOT EXISTS strategy_equity (
                    ts TEXT PRIMARY KEY,
                    equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    lifecycle TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_series (
                    series_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    label TEXT NOT NULL,
                    candidate_type TEXT NOT NULL,
                    account_ref TEXT,
                    params_json TEXT NOT NULL,
                    equity REAL NOT NULL,
                    return_pct REAL,
                    PRIMARY KEY(series_id,ts)
                );
                CREATE TABLE IF NOT EXISTS auto_order_intents (
                    intent_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    config_version INTEGER NOT NULL,
                    signal_batch_id TEXT NOT NULL,
                    signal_source_date TEXT NOT NULL,
                    factor_set_hash TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    purpose TEXT NOT NULL,
                    target_qty REAL NOT NULL CHECK(target_qty>=0),
                    order_qty REAL NOT NULL CHECK(order_qty>0),
                    limit_price REAL NOT NULL CHECK(limit_price>0),
                    status TEXT NOT NULL CHECK(status IN
                        ('PLANNED','RESERVED','DISPATCHING','ACKED','PARTIAL',
                         'FILLED','CANCELLED','FAILED','UNKNOWN')),
                    preview_id TEXT,
                    reserved_notional REAL NOT NULL CHECK(reserved_notional>=0),
                    reserved_sell_qty REAL NOT NULL CHECK(reserved_sell_qty>=0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS auto_order_intents_status
                    ON auto_order_intents(status,created_at);
                CREATE TABLE IF NOT EXISTS execution_holds (
                    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_type TEXT NOT NULL CHECK(scope_type IN ('SYSTEM','SYMBOL','INTENT')),
                    scope_key TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolved_by TEXT,
                    resolution_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_execution_hold
                    ON execution_holds(scope_type,scope_key,reason_code,source)
                    WHERE resolved_at IS NULL;
                CREATE INDEX IF NOT EXISTS execution_holds_active_scope
                    ON execution_holds(resolved_at,scope_type,scope_key);
            """)
            columns = {row[1] for row in con.execute("PRAGMA table_info(strategy_state)")}
            if "required_sync_after" not in columns:
                con.execute("ALTER TABLE strategy_state ADD COLUMN required_sync_after TEXT")
                con.execute("UPDATE strategy_state SET required_sync_after=updated_at")
            proof_columns = {row[1] for row in con.execute("PRAGMA table_info(broker_sync_proof)")}
            if "control_generation" not in proof_columns:
                con.execute("ALTER TABLE broker_sync_proof ADD COLUMN control_generation INTEGER NOT NULL DEFAULT 0")
            fill_columns = {row[1] for row in con.execute("PRAGMA table_info(applied_fills)")}
            if "fee_is_stable" not in fill_columns:
                con.execute("ALTER TABLE applied_fills ADD COLUMN fee_is_stable INTEGER NOT NULL DEFAULT 0")
            if "order_hash" not in fill_columns:
                con.execute("ALTER TABLE applied_fills ADD COLUMN order_hash TEXT")
            intent_columns = {row[1] for row in con.execute("PRAGMA table_info(auto_order_intents)")}
            if "intent_key" not in intent_columns:
                con.execute("ALTER TABLE auto_order_intents ADD COLUMN intent_key TEXT")
                con.execute("UPDATE auto_order_intents SET intent_key=intent_id WHERE intent_key IS NULL")
            if "attempt_no" not in intent_columns:
                con.execute("ALTER TABLE auto_order_intents ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1")
            if "retry_of" not in intent_columns:
                con.execute("ALTER TABLE auto_order_intents ADD COLUMN retry_of TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS auto_order_intents_key_attempt "
                        "ON auto_order_intents(intent_key,attempt_no DESC)")
            con.execute("PRAGMA user_version=2")
            exists = con.execute("SELECT 1 FROM strategy_state WHERE id=1").fetchone()
            if not exists:
                now = utcnow()
                con.execute("""INSERT INTO strategy_state
                    (id,lifecycle,freeze_latched,freeze_reason,strategy_id,initial_capital,
                     exposure_cap,loss_floor,allocated_cash,owned_market_value,strategy_equity,
                     realized_pnl,unrealized_pnl,reserved_buy_notional,config_version,last_sync_at,
                     created_at,updated_at,required_sync_after)
                    VALUES(1,'FROZEN',1,'not_provisioned',?,10000.0,10000.0,7500.0,
                     10000.0,0.0,10000.0,0.0,0.0,0.0,1,NULL,?,?,?)""",
                            (DEFAULT_CONFIG["strategy_id"], now, now, now))
                con.execute("INSERT INTO strategy_config VALUES(1,1,?,?,?,?)",
                            (json.dumps(DEFAULT_CONFIG, sort_keys=True), now, "bootstrap", "safe default"))
                self._event_tx(con, "system_initialized", "system", "warning",
                               "Live strategy initialized frozen", {})
            persisted = con.execute(
                "SELECT lifecycle,freeze_reason FROM strategy_state WHERE id=1"
            ).fetchone()
            if persisted and persisted["freeze_reason"]:
                safe_reason, changed = _safe_freeze_reason(persisted["freeze_reason"])
                if changed:
                    con.execute(
                        "UPDATE strategy_state SET freeze_reason=?,updated_at=? WHERE id=1",
                        (safe_reason, utcnow()),
                    )
                if (persisted["lifecycle"] == "FROZEN"
                        and safe_reason in _LEGACY_AUTOMATIC_FREEZE_CODES):
                    now = utcnow()
                    con.execute("INSERT OR IGNORE INTO execution_holds "
                                "(scope_type,scope_key,reason_code,source,created_at) "
                                "VALUES('SYSTEM','*',?,'moomoo_sync',?)",
                                (safe_reason.upper(), now))
                    con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                                "freeze_reason=NULL,updated_at=? WHERE id=1", (now,))
                    self._event_tx(con, "legacy_automatic_freeze_migrated", "migration", "warning",
                                   "Legacy automatic freeze converted to an execution hold",
                                   {"reason_code": safe_reason, "scope_type": "SYSTEM"})

    def _event_tx(self, con: sqlite3.Connection, event_type: str, source: str,
                  severity: str, message: str, details: dict[str, Any]) -> int:
        row = con.execute("SELECT config_version FROM strategy_state WHERE id=1").fetchone()
        cur = con.execute("""INSERT INTO strategy_events
            (ts,event_type,source,severity,message,details_json,config_version)
            VALUES(?,?,?,?,?,?,?)""",
            (utcnow(), _redact(event_type), _redact(source), severity, _redact(message),
             json.dumps(_redact(details), ensure_ascii=False, sort_keys=True, default=str),
             int(row[0]) if row else None))
        return int(cur.lastrowid or 0)

    def _latch_fill_conflict_tx(
        self, con: sqlite3.Connection, symbol: str,
        replay: tuple[str, str, float, float, float],
        persisted: tuple[str, str, float, float, float],
        extra_fields: tuple[str, ...] = (),
    ) -> None:
        """Latch an immutable-fill conflict without persisting Broker identifiers."""
        now = utcnow()
        self._create_execution_hold_tx(con, "SYMBOL", symbol,
                                       "RECONCILIATION_FILL_CONFLICT", "moomoo_reconciler")
        con.execute("DELETE FROM broker_sync_proof")
        field_names = ("symbol", "side", "quantity", "price", "fee")
        conflicting_fields = [
            name for name, old, new in zip(field_names, persisted, replay) if old != new
        ]
        conflicting_fields.extend(
            field for field in extra_fields
            if field == "order_ownership" and field not in conflicting_fields
        )
        self._event_tx(
            con, "reconciliation_fill_conflict", "moomoo_reconciler", "critical",
            "Immutable fill reference was replayed with conflicting economics",
            {
                "symbol": symbol,
                "conflicting_fields": conflicting_fields,
            },
        )

    def latch_snapshot_deal_conflict(self, symbol: str, conflicting_fields: list[str]) -> None:
        """Commit a non-recoverable, secret-free same-snapshot conflict latch."""
        safe_symbol = str(symbol or "").strip().upper()
        requested = set(conflicting_fields)
        safe_fields = [
            field for field in (
                "symbol", "side", "quantity", "price", "fee", "deal_identity",
            )
            if field in requested
        ]
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = utcnow()
            self._create_execution_hold_tx(
                con, "SYMBOL" if safe_symbol else "SYSTEM", safe_symbol or "*",
                "RECONCILIATION_SNAPSHOT_DEAL_CONFLICT", "moomoo_reconciler",
            )
            con.execute("DELETE FROM broker_sync_proof")
            self._event_tx(
                con, "reconciliation_snapshot_deal_conflict", "moomoo_reconciler", "critical",
                "Broker snapshot repeated a deal reference with conflicting economics",
                {"symbol": safe_symbol, "conflicting_fields": safe_fields},
            )
            con.commit()
        raise ControlRejected("Moomoo returned a conflicting duplicate deal reference")

    def latch_reconciliation_snapshot_conflict(
        self, category: str, symbol: str, conflicting_fields: list[str], message: str,
    ) -> None:
        """Atomically persist a classified, non-recoverable snapshot conflict."""
        event_type = {
            "numeric": "reconciliation_snapshot_numeric_conflict",
            "order_economics": "reconciliation_snapshot_order_conflict",
            "positions": "reconciliation_snapshot_position_conflict",
        }.get(str(category))
        if event_type is None:
            raise ValueError("Unsupported reconciliation conflict category")
        safe_symbol = str(symbol or "").strip().upper()
        requested = set(conflicting_fields)
        safe_fields = [field for field in (
            "symbol", "side", "quantity", "price", "fee", "order_ownership",
            "deal_identity",
        )
                       if field in requested]
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = utcnow()
            self._create_execution_hold_tx(
                con, "SYMBOL" if safe_symbol else "SYSTEM", safe_symbol or "*",
                event_type.upper(), "moomoo_reconciler",
            )
            con.execute("DELETE FROM broker_sync_proof")
            self._event_tx(
                con, event_type, "moomoo_reconciler", "critical", message,
                {"symbol": safe_symbol, "conflicting_fields": safe_fields},
            )
            con.commit()

    def _invalidate_runtime_tx(self, con: sqlite3.Connection, reason: str) -> int:
        row = con.execute("SELECT generation FROM control_runtime WHERE id=1").fetchone()
        generation = (int(row["generation"]) + 1) if row else 1
        now = utcnow()
        pending = f"pending:{generation}:{reason}"
        con.execute("INSERT INTO control_runtime VALUES(1,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,"
                    "fingerprint=excluded.fingerprint,changed_at=excluded.changed_at",
                    (generation, pending, now))
        con.execute("DELETE FROM broker_sync_proof")
        self._create_execution_hold_tx(con, "SYSTEM", "*",
                                       "CONTROL_GENERATION_CHANGED_REQUIRES_SYNC", "control")
        self._event_tx(con, "control_generation_invalidated", "control", "critical",
                       "Control generation changed; broker sync proof invalidated",
                       {"generation": generation, "reason": reason})
        return generation

    def event(self, event_type: str, source: str, severity: str,
              message: str, details: dict[str, Any] | None = None) -> int:
        with self.connect() as con:
            return self._event_tx(con, event_type, source, severity, message, details or {})

    @staticmethod
    def _hold_identity(scope_type: str, scope_key: str, reason_code: str,
                       source: str) -> tuple[str, str, str, str]:
        scope = str(scope_type).strip().upper()
        key = str(scope_key or "").strip().upper() if scope == "SYMBOL" else str(scope_key or "").strip()
        reason = str(reason_code or "").strip().upper()
        owner = str(source or "").strip()
        if scope not in EXECUTION_HOLD_SCOPES:
            raise ControlRejected("Invalid execution hold scope")
        if scope == "SYSTEM":
            key = "*"
        if not key or not _INTENT_ERROR_CODE.fullmatch(reason) or not owner or len(owner) > 80:
            raise ControlRejected("Invalid execution hold identity")
        return scope, key, reason, owner

    def _create_execution_hold_tx(self, con: sqlite3.Connection, scope_type: str,
                                  scope_key: str, reason_code: str, source: str) -> dict[str, Any]:
        scope, key, reason, owner = self._hold_identity(scope_type, scope_key, reason_code, source)
        now = utcnow()
        cur = con.execute("INSERT OR IGNORE INTO execution_holds "
                          "(scope_type,scope_key,reason_code,source,created_at) VALUES(?,?,?,?,?)",
                          (scope, key, reason, owner, now))
        row = con.execute("SELECT * FROM execution_holds WHERE scope_type=? AND scope_key=? "
                          "AND reason_code=? AND source=? AND resolved_at IS NULL",
                          (scope, key, reason, owner)).fetchone()
        if cur.rowcount:
            self._event_tx(con, "execution_hold_active", owner, "critical",
                           "Execution hold is active",
                           {"scope_type": scope, "scope_key": key, "reason_code": reason})
        return dict(row)

    def create_execution_hold(self, scope_type: str, scope_key: str, reason_code: str,
                              source: str) -> dict[str, Any]:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            return self._create_execution_hold_tx(con, scope_type, scope_key, reason_code, source)

    def latch_auto_intent_hold(self, intent_id: str, reason_code: str, *,
                               mark_unknown: bool = False) -> dict[str, Any]:
        """Atomically fail closed an automatic intent and invalidate sync proof."""
        reason = str(reason_code or "").strip().upper()
        if not _INTENT_ERROR_CODE.fullmatch(reason):
            raise ControlRejected("Invalid automatic intent hold reason")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM auto_order_intents WHERE intent_id=?",
                              (str(intent_id),)).fetchone()
            if not row:
                raise ControlRejected("Auto intent not found")
            current = str(row["status"])
            if mark_unknown and current != "UNKNOWN":
                if "UNKNOWN" not in self._AUTO_TRANSITIONS[current]:
                    raise ControlRejected(f"Illegal auto intent transition {current}->UNKNOWN")
                con.execute("UPDATE auto_order_intents SET status='UNKNOWN',error_code=?,updated_at=? "
                            "WHERE intent_id=?", (reason, utcnow(), str(intent_id)))
            self._create_execution_hold_tx(con, "INTENT", str(intent_id), reason, "auto_executor")
            con.execute("DELETE FROM broker_sync_proof")
            self._event_tx(con, "auto_intent_execution_held", "auto_executor", "critical",
                           "Automatic intent requires reconciliation proof",
                           {"intent_id_hash": str(intent_id), "reason_code": reason})
            updated = con.execute("SELECT * FROM auto_order_intents WHERE intent_id=?",
                                  (str(intent_id),)).fetchone()
            return self._auto_intent_dict(updated)

    def resolve_execution_holds(self, *, scope_type: str, scope_key: str,
                                reason_code: str, source: str,
                                resolved_by: str, resolution_reason: str) -> int:
        scope, key, reason, owner = self._hold_identity(scope_type, scope_key, reason_code, source)
        if not str(resolved_by or "").strip() or not str(resolution_reason or "").strip():
            raise ControlRejected("Execution hold resolution requires actor and reason")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = utcnow()
            cur = con.execute("UPDATE execution_holds SET resolved_at=?,resolved_by=?,resolution_reason=? "
                              "WHERE scope_type=? AND scope_key=? AND reason_code=? AND source=? "
                              "AND resolved_at IS NULL",
                              (now, str(resolved_by), str(resolution_reason), scope, key, reason, owner))
            count = int(cur.rowcount)
            if count:
                self._event_tx(con, "execution_hold_resolved", str(resolved_by), "info",
                               "Matching execution hold was resolved",
                               {"scope_type": scope, "scope_key": key, "reason_code": reason,
                                "source": owner})
            return count

    def list_execution_holds(self, *, active_only: bool = False, limit: int = 1000) -> list[dict[str, Any]]:
        query = "SELECT * FROM execution_holds"
        if active_only:
            query += " WHERE resolved_at IS NULL"
        query += " ORDER BY hold_id DESC LIMIT ?"
        with self.connect() as con:
            rows = con.execute(query, (max(1, min(int(limit), 5000)),)).fetchall()
        return [dict(row) for row in rows]

    def applicable_execution_holds(self, *, symbol: str | None = None,
                                   intent_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["(scope_type='SYSTEM' AND scope_key='*')"]
        params: list[str] = []
        if symbol:
            clauses.append("(scope_type='SYMBOL' AND scope_key=?)")
            params.append(str(symbol).strip().upper())
        if intent_id:
            clauses.append("(scope_type='INTENT' AND scope_key=?)")
            params.append(str(intent_id).strip())
        with self.connect() as con:
            rows = con.execute("SELECT * FROM execution_holds WHERE resolved_at IS NULL AND (" +
                               " OR ".join(clauses) + ") ORDER BY hold_id", tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def execution_status(self) -> dict[str, Any]:
        holds = self.list_execution_holds(active_only=True)
        state = self.snapshot()
        unresolved = [row for row in self.list_auto_order_intents(limit=1000)
                      if row["status"] in AUTO_INTENT_UNRESOLVED]
        executable = state.lifecycle == "ACTIVE" and not holds and not unresolved
        return {"status": "READY" if executable else "HELD",
                "executable": executable, "active_holds": holds,
                "unresolved_intent_count": len(unresolved)}

    def snapshot(self) -> RiskSnapshot:
        with self.connect() as con:
            row = con.execute("SELECT * FROM strategy_state WHERE id=1").fetchone()
        if not row:
            raise ControlRejected("Strategy control state is missing")
        safe_freeze_reason = (
            _safe_freeze_reason(row["freeze_reason"])[0] if row["freeze_reason"] else None
        )
        return RiskSnapshot(
            lifecycle=row["lifecycle"], frozen=row["lifecycle"] != "ACTIVE",
            freeze_reason=safe_freeze_reason, initial_capital=row["initial_capital"],
            exposure_cap=row["exposure_cap"], loss_floor=row["loss_floor"],
            allocated_cash=row["allocated_cash"], owned_market_value=row["owned_market_value"],
            strategy_equity=row["strategy_equity"], realized_pnl=row["realized_pnl"],
            unrealized_pnl=row["unrealized_pnl"], reserved_buy_notional=row["reserved_buy_notional"],
            config_version=row["config_version"], strategy_id=row["strategy_id"],
            last_sync_at=row["last_sync_at"], required_sync_after=row["required_sync_after"],
        )

    def config(self) -> dict[str, Any]:
        with self.connect() as con:
            row = con.execute("SELECT version,config_json,changed_at,changed_by,reason "
                              "FROM strategy_config WHERE active=1").fetchone()
        if not row:
            raise ControlRejected("No active strategy configuration")
        return {"version": row["version"], "values": json.loads(row["config_json"]),
                "changed_at": row["changed_at"], "changed_by": row["changed_by"],
                "reason": row["reason"]}

    @staticmethod
    def validate_config(candidate: dict[str, Any]) -> dict[str, Any]:
        unknown = set(candidate) - set(DEFAULT_CONFIG)
        if unknown:
            raise ControlRejected("Unknown parameters: " + ", ".join(sorted(unknown)))
        merged = {**DEFAULT_CONFIG, **candidate}
        if not str(merged["strategy_id"]).strip():
            raise ControlRejected("strategy_id is required")
        integer_ranges = {
            "top_n": (1, 20), "stop_cooldown_hours": (0, 720),
            "min_hold_days": (0, 30), "hold_band_mult": (1, 10),
            "rebalance_hours": (1, 168), "max_quote_age_seconds": (10, 300),
        }
        for name, (low, high) in integer_ranges.items():
            value = merged[name]
            if isinstance(value, bool) or int(value) != value or not low <= int(value) <= high:
                raise ControlRejected(f"Invalid {name}")
            merged[name] = int(value)
        float_ranges = {
            "position_target_pct": (0.01, 1.0), "gross_target_pct": (0.05, 0.95),
            "stop_loss_pct": (0.01, 0.30), "max_order_notional": (1, 2500),
            "max_daily_order_notional": (1, 5000),
            "max_limit_deviation_pct": (0.001, 0.05),
        }
        for name, (low, high) in float_ranges.items():
            value = _finite(merged[name], name)
            if not low <= value <= high:
                raise ControlRejected(f"Invalid {name}")
            merged[name] = value
        if merged["top_n"] * merged["position_target_pct"] > merged["gross_target_pct"] + 1e-9:
            raise ControlRejected("top_n × position_target_pct exceeds gross_target_pct")
        if merged["gross_target_pct"] * INITIAL_CAPITAL > EXPOSURE_CAP:
            raise ControlRejected("Target exposure exceeds immutable USD 10,000 cap")
        return merged

    def update_config(self, patch: dict[str, Any], expected_version: int,
                      changed_by: str, reason: str) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise ControlRejected("A change reason is required")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            state = con.execute("SELECT lifecycle,config_version FROM strategy_state WHERE id=1").fetchone()
            if int(state["config_version"]) != int(expected_version):
                raise ControlRejected("Configuration changed; reload before editing")
            active = con.execute("SELECT config_json FROM strategy_config WHERE active=1").fetchone()
            current = json.loads(active[0])
            if "strategy_id" in patch and patch["strategy_id"] != current["strategy_id"]:
                raise ControlRejected("strategy_id cannot be hot-edited; clean and provision a new strategy")
            candidate = self.validate_config({**current, **patch})
            version = int(expected_version) + 1
            now = utcnow()
            con.execute("UPDATE strategy_config SET active=0 WHERE active=1")
            con.execute("INSERT INTO strategy_config VALUES(1+?,1,?,?,?,?)",
                        (int(expected_version), json.dumps(candidate, sort_keys=True), now,
                         str(changed_by or "dashboard"), reason))
            con.execute("UPDATE strategy_state SET config_version=?,strategy_id=?,updated_at=? WHERE id=1",
                        (version, candidate["strategy_id"], now))
            self._invalidate_runtime_tx(con, "strategy_config_changed")
            self._event_tx(con, "config_reloaded", "dashboard", "warning",
                           "Strategy parameters hot-reloaded; execution held for review",
                           {"version": version, "changed_fields": sorted(patch), "reason": reason})
        return self.config()

    def freeze(self, reason: str, source: str = "dashboard", severity: str = "critical",
               *, preserve_existing: bool = False) -> RiskSnapshot:
        if str(source) not in _OPERATOR_ACTORS:
            raise ControlRejected("FROZEN is an operator-only lifecycle control")
        raw_reason = str(reason or "").strip() or "manual_freeze"
        reason, reason_was_sanitized = _safe_freeze_reason(raw_reason, source)
        now = utcnow()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute(
                "SELECT lifecycle,freeze_reason,required_sync_after FROM strategy_state WHERE id=1"
            ).fetchone()
            already_latched = bool(
                preserve_existing and current and current["lifecycle"] == "FROZEN"
                and current["freeze_reason"]
            )
            if reason_was_sanitized:
                self._event_tx(
                    con, "freeze_reason_sanitized", source, "warning",
                    "Freeze request detail was sanitized to a bounded reason code",
                    {"reason_code": reason},
                )
            if not already_latched:
                required = (current["required_sync_after"] if current
                            and current["lifecycle"] == "FROZEN"
                            and current["freeze_reason"] == reason else now)
                con.execute("UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
                            "freeze_reason=?,updated_at=?,required_sync_after=? WHERE id=1",
                            (reason, now, required))
                self._event_tx(con, "system_frozen", source, severity, "Trading system frozen", {"reason": reason})
        return self.snapshot()

    def unfreeze(self, reason: str, actor: str = "dashboard") -> RiskSnapshot:
        if str(actor) not in _OPERATOR_ACTORS:
            raise ControlRejected("FROZEN is an operator-only lifecycle control")
        reason = str(reason or "").strip()
        if not reason:
            raise ControlRejected("An unfreeze reason is required")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM strategy_state WHERE id=1").fetchone()
            if row["lifecycle"] in {"UNCONFIGURED", "CLEANED"} or not row["strategy_id"]:
                raise ControlRejected("No valid strategy is provisioned")
            if row["strategy_equity"] <= row["loss_floor"]:
                raise ControlRejected("Loss-floor freeze cannot be released while equity is at or below USD 7,500")
            if not row["last_sync_at"]:
                raise ControlRejected("A successful Moomoo reconciliation is required before unfreezing")
            try:
                synced = datetime.fromisoformat(str(row["last_sync_at"]).replace("Z", "+00:00"))
                if synced.tzinfo is None:
                    synced = synced.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - synced.astimezone(timezone.utc)).total_seconds()
                if age < -60 or age > 7 * 60:
                    raise ValueError
                required_raw = row["required_sync_after"]
                if required_raw:
                    required = datetime.fromisoformat(str(required_raw).replace("Z", "+00:00"))
                    if required.tzinfo is None:
                        required = required.replace(tzinfo=timezone.utc)
                    if synced.astimezone(timezone.utc) <= required.astimezone(timezone.utc):
                        raise ControlRejected("A post-freeze Moomoo reconciliation is required")
            except ValueError:
                raise ControlRejected("A fresh Moomoo reconciliation within 7 minutes is required")
            if con.execute("SELECT 1 FROM manual_symbol_conflicts LIMIT 1").fetchone():
                raise ControlRejected("Persistent manual broker activity conflict requires cleanup/reprovision")
            proof = con.execute("SELECT fingerprint,synced_at,control_generation "
                                "FROM broker_sync_proof WHERE id=1").fetchone()
            runtime = con.execute("SELECT generation,fingerprint FROM control_runtime WHERE id=1").fetchone()
            if (not proof or not runtime
                    or int(proof["control_generation"]) != int(runtime["generation"])
                    or not hmac.compare_digest(str(proof["fingerprint"]), str(runtime["fingerprint"]))
                    or str(proof["synced_at"]) != str(row["last_sync_at"])):
                raise ControlRejected("Current account isolation generation lacks a matching broker sync proof")
            con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                        "freeze_reason=NULL,updated_at=? WHERE id=1", (utcnow(),))
            self._event_tx(con, "system_unfrozen", actor, "critical", "Trading system unfrozen", {"reason": reason})
        return self.snapshot()

    def positions(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM owned_positions WHERE quantity>0 ORDER BY market_value DESC").fetchall()
        return [dict(row) for row in rows]

    def fills(self, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self.connect() as con:
            rows = con.execute(
                "SELECT symbol,side,quantity,price,fee,applied_at "
                "FROM applied_fills ORDER BY applied_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def fill_display_history(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return immutable fills plus a display-only reconciled fee allocation.

        ``fee`` remains the deal-row value. ``effective_fee`` allocates an
        order-level cumulative Broker fee across that order's fills by traded
        notional. Accounting does not consume this presentation field.
        """
        safe_limit = max(1, min(int(limit), 1000))
        with self.connect() as con:
            rows = con.execute(
                "WITH fee_view AS ("
                " SELECT f.symbol,f.side,f.quantity,f.price,f.fee,f.applied_at,"
                " f.order_hash,a.cumulative_fee,a.finalized,"
                " SUM(f.quantity*f.price) OVER (PARTITION BY f.order_hash) AS order_notional"
                " FROM applied_fills f LEFT JOIN order_fee_accounts a"
                " ON a.order_hash=f.order_hash"
                ") SELECT symbol,side,quantity,price,fee,"
                " CASE WHEN cumulative_fee IS NOT NULL AND order_notional>0"
                " THEN cumulative_fee*(quantity*price)/order_notional ELSE fee END AS effective_fee,"
                " CASE WHEN cumulative_fee IS NOT NULL THEN finalized ELSE 1 END AS fee_finalized,"
                " applied_at FROM fee_view ORDER BY applied_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def execution_summary(self) -> dict[str, Any]:
        with self.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS total_trades, "
                "COALESCE(SUM(fee),0) AS fill_fees, "
                "COALESCE(SUM(quantity*price),0) AS total_notional, "
                "COALESCE(SUM(CASE WHEN side='BUY' THEN 1 ELSE 0 END),0) AS buy_trades, "
                "COALESCE(SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END),0) AS sell_trades, "
                "MIN(applied_at) AS first_trade_at, MAX(applied_at) AS last_trade_at "
                "FROM applied_fills"
            ).fetchone()
            adjustment_row = con.execute(
                "SELECT COALESCE(SUM(delta),0) AS adjustment_fees FROM order_fee_adjustments"
            ).fetchone()
        return {
            "total_trades": int(row["total_trades"]),
            "total_fees": float(row["fill_fees"]) + float(adjustment_row["adjustment_fees"]),
            "total_notional": float(row["total_notional"]),
            "buy_trades": int(row["buy_trades"]),
            "sell_trades": int(row["sell_trades"]),
            "first_trade_at": row["first_trade_at"],
            "last_trade_at": row["last_trade_at"],
        }

    def symbol_performance(self) -> list[dict[str, Any]]:
        """Return all traded/current symbols ranked by net lifetime P&L.

        Net P&L is reconstructed from immutable strategy fills, the latest
        cumulative Broker fee account for each order, and current strategy-only
        market value.  This keeps closed symbols visible and prevents delayed
        fee updates from disappearing from per-symbol performance.
        """
        with self.connect() as con:
            fills = con.execute(
                "SELECT symbol,side,quantity,price,fee,order_hash,applied_at "
                "FROM applied_fills ORDER BY applied_at,fill_hash"
            ).fetchall()
            positions = con.execute(
                "SELECT symbol,quantity,average_cost,market_price,market_value,"
                "realized_pnl,updated_at FROM owned_positions"
            ).fetchall()
            fee_accounts = con.execute(
                "SELECT order_hash,symbol,side,cumulative_fee FROM order_fee_accounts"
            ).fetchall()

        rows: dict[str, dict[str, Any]] = {}

        def item(symbol: Any) -> dict[str, Any]:
            normalized = str(symbol or "").strip().upper()
            if not normalized:
                raise ControlRejected("Invalid symbol performance identity")
            return rows.setdefault(normalized, {
                "symbol": normalized,
                "quantity": 0.0,
                "average_cost": 0.0,
                "market_price": 0.0,
                "market_value": 0.0,
                "buy_quantity": 0.0,
                "sell_quantity": 0.0,
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "fees": 0.0,
                "buy_fees": 0.0,
                "first_trade_at": None,
                "last_trade_at": None,
                "position_updated_at": None,
            })

        fee_order_hashes: set[str] = set()
        for account in fee_accounts:
            symbol = str(account["symbol"] or "").strip().upper()
            side = str(account["side"] or "").strip().upper()
            fee = _finite(account["cumulative_fee"], "cumulative_fee")
            order_hash = str(account["order_hash"] or "").strip()
            if not order_hash or side not in {"BUY", "SELL"} or fee < 0:
                raise ControlRejected("Invalid order fee account")
            target = item(symbol)
            target["fees"] += fee
            if side == "BUY":
                target["buy_fees"] += fee
            fee_order_hashes.add(order_hash)

        for fill in fills:
            target = item(fill["symbol"])
            side = str(fill["side"] or "").strip().upper()
            quantity = _finite(fill["quantity"], "quantity")
            price = _finite(fill["price"], "price")
            fee = _finite(fill["fee"], "fee")
            if side not in {"BUY", "SELL"} or quantity <= 0 or price < 0 or fee < 0:
                raise ControlRejected("Invalid historical fill economics")
            notional = quantity * price
            target[f"{side.lower()}_quantity"] += quantity
            target[f"{side.lower()}_notional"] += notional
            order_hash = str(fill["order_hash"] or "").strip()
            if not order_hash or order_hash not in fee_order_hashes:
                target["fees"] += fee
                if side == "BUY":
                    target["buy_fees"] += fee
            applied_at = str(fill["applied_at"] or "") or None
            if applied_at:
                target["first_trade_at"] = target["first_trade_at"] or applied_at
                target["last_trade_at"] = applied_at

        for position in positions:
            target = item(position["symbol"])
            quantity = _finite(position["quantity"], "position_quantity")
            average_cost = _finite(position["average_cost"], "average_cost")
            market_price = _finite(position["market_price"], "market_price")
            market_value = _finite(position["market_value"], "market_value")
            if quantity < 0 or average_cost < 0 or market_price < 0 or market_value < 0:
                raise ControlRejected("Invalid position performance economics")
            target.update({
                "quantity": quantity,
                "average_cost": average_cost,
                "market_price": market_price,
                "market_value": market_value,
                "position_updated_at": position["updated_at"],
            })

        result: list[dict[str, Any]] = []
        for target in rows.values():
            total_pnl = (target["sell_notional"] - target["buy_notional"]
                         - target["fees"] + target["market_value"])
            unrealized_pnl = (
                target["market_value"] - target["quantity"] * target["average_cost"]
                if target["quantity"] > 0 else 0.0
            )
            realized_pnl = total_pnl - unrealized_pnl
            deployed = target["buy_notional"] + target["buy_fees"]
            target.update({
                "holding": target["quantity"] > 0,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "total_pnl": total_pnl,
                "return_pct": (total_pnl / deployed * 100.0) if deployed > 0 else None,
            })
            result.append(target)
        result.sort(key=lambda row: (-float(row["total_pnl"]), str(row["symbol"])))
        return result

    def performance_summary(self) -> dict[str, Any]:
        state = self.snapshot()
        daily_closes: dict[str, float] = {}
        for row in self.equity_history():
            equity = float(row["equity"])
            if math.isfinite(equity) and equity > 0:
                daily_closes[str(row["ts"])[:10]] = equity
        values = list(daily_closes.values())
        daily_returns = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))]
        sharpe = None
        if len(daily_returns) >= 20:
            mean = sum(daily_returns) / len(daily_returns)
            variance = sum((value - mean) ** 2 for value in daily_returns) / (len(daily_returns) - 1)
            if variance > 0:
                sharpe = mean / math.sqrt(variance) * math.sqrt(252)
        peak = INITIAL_CAPITAL
        max_drawdown = 0.0
        for equity in [INITIAL_CAPITAL, *values]:
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity / peak - 1.0)
        return {
            "pnl": float(state.strategy_equity - INITIAL_CAPITAL),
            "total_return_pct": float((state.strategy_equity / INITIAL_CAPITAL - 1.0) * 100),
            "sharpe_ratio": float(sharpe) if sharpe is not None and math.isfinite(sharpe) else None,
            "sharpe_observations": len(daily_returns),
            "max_drawdown_pct": float(max_drawdown * 100),
        }

    def owned_quantity(self, symbol: str) -> float:
        with self.connect() as con:
            row = con.execute("SELECT quantity FROM owned_positions WHERE symbol=?", (symbol.upper(),)).fetchone()
        return float(row[0]) if row else 0.0

    @staticmethod
    def _auto_intent_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def get_auto_order_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM auto_order_intents WHERE intent_id=?", (str(intent_id),)
            ).fetchone()
        return self._auto_intent_dict(row) if row else None

    def list_auto_order_intents(self, status: str | None = None,
                                limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        params: tuple[Any, ...]
        query = "SELECT * FROM auto_order_intents"
        if status is not None:
            normalized = str(status).upper()
            if normalized not in AUTO_INTENT_STATUSES:
                raise ControlRejected("Invalid auto intent status")
            query += " WHERE status=?"
            params = (normalized, safe_limit)
        else:
            params = (safe_limit,)
        query += " ORDER BY created_at DESC,intent_id DESC LIMIT ?"
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
        return [self._auto_intent_dict(row) for row in rows]

    def auto_intent_reservations(self, exclude_intent_id: str | None = None) -> dict[str, Any]:
        """Return nonterminal reservations for a final, race-safe order preview."""
        query = ("SELECT symbol,reserved_notional,reserved_sell_qty FROM auto_order_intents "
                 "WHERE status NOT IN ('FILLED','CANCELLED','FAILED')")
        params: tuple[Any, ...] = ()
        if exclude_intent_id is not None:
            query += " AND intent_id<>?"
            params = (str(exclude_intent_id),)
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
        sell: dict[str, float] = {}
        buy = 0.0
        for row in rows:
            buy += float(row["reserved_notional"])
            quantity = float(row["reserved_sell_qty"])
            if quantity > 0:
                symbol = str(row["symbol"])
                sell[symbol] = sell.get(symbol, 0.0) + quantity
        return {"reserved_buy_notional": buy, "reserved_sell_qty": sell}

    def create_auto_order_intent(
        self, *, strategy_id: str, config_version: int, signal_batch_id: str,
        signal_source_date: str, factor_set_hash: str, symbol: str, side: str,
        purpose: str, target_qty: float, order_qty: float, limit_price: float,
        broker_pending_buy_notional: float = 0.0,
        broker_pending_sell_qty: float = 0.0,
        daily_order_notional: float = 0.0,
    ) -> dict[str, Any]:
        """Atomically create and reserve a deterministic at-most-once intent."""
        strategy_id = str(strategy_id).strip()
        signal_batch_id = str(signal_batch_id).strip()
        signal_source_date = str(signal_source_date).strip()
        factor_set_hash = str(factor_set_hash).strip().lower()
        symbol = str(symbol).strip().upper()
        side = str(side).strip().upper()
        purpose = str(purpose).strip().upper()
        if not all((strategy_id, signal_batch_id, signal_source_date,
                    factor_set_hash, symbol, purpose)):
            raise ControlRejected("Auto intent identity fields are required")
        if strategy_id != "B16" or not symbol.startswith("US."):
            raise ControlRejected("Auto intents are restricted to B16 US symbols")
        if not _HEX64.fullmatch(signal_batch_id.lower()) or not _HEX64.fullmatch(factor_set_hash):
            raise ControlRejected("Auto intent signal hashes must be SHA-256")
        if side not in {"BUY", "SELL"}:
            raise ControlRejected("Only BUY and SELL are supported")
        allowed_purposes = {"TARGET_BUY": "BUY", "RANK_EXIT": "SELL", "STOP_LOSS": "SELL"}
        if purpose not in allowed_purposes or allowed_purposes[purpose] != side:
            raise ControlRejected("Auto intent purpose does not match side")
        if isinstance(config_version, bool):
            raise ControlRejected("Invalid config_version")
        raw_config_version = config_version
        try:
            config_version = int(raw_config_version)
        except (TypeError, ValueError) as exc:
            raise ControlRejected("Invalid config_version") from exc
        if isinstance(raw_config_version, float) and raw_config_version != config_version:
            raise ControlRejected("Invalid config_version")
        if isinstance(raw_config_version, str) and raw_config_version.strip() != str(config_version):
            raise ControlRejected("Invalid config_version")
        target_qty = _finite(target_qty, "target_qty")
        order_qty = _finite(order_qty, "order_qty")
        limit_price = _finite(limit_price, "limit_price")
        broker_pending_buy_notional = _finite(
            broker_pending_buy_notional, "broker_pending_buy_notional"
        )
        broker_pending_sell_qty = _finite(
            broker_pending_sell_qty, "broker_pending_sell_qty"
        )
        daily_order_notional = _finite(daily_order_notional, "daily_order_notional")
        if (target_qty < 0 or order_qty <= 0 or limit_price <= 0
                or broker_pending_buy_notional < 0 or broker_pending_sell_qty < 0
                or daily_order_notional < 0):
            raise ControlRejected("Invalid auto intent quantity, price, or pending reservation")
        if target_qty != int(target_qty) or order_qty != int(order_qty):
            raise ControlRejected("US auto intents require whole-share quantities")

        key_payload = {
            "config_version": config_version, "purpose": purpose, "side": side,
            "signal_batch_id": signal_batch_id, "strategy_id": strategy_id,
            "symbol": symbol, "target_qty": target_qty, "order_qty": order_qty,
        }
        payload = {
            **key_payload, "signal_source_date": signal_source_date,
            "factor_set_hash": factor_set_hash,
            "limit_price": limit_price,
        }
        canonical_key = json.dumps(key_payload, sort_keys=True, separators=(",", ":"))
        canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        intent_key = hashlib.sha256(canonical_key.encode()).hexdigest()
        intent_id = intent_key
        payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        conflict = False
        result: dict[str, Any] | None = None
        attempt_no = 1
        retry_of: str | None = None

        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM auto_order_intents WHERE intent_key=? OR "
                "(intent_key IS NULL AND intent_id=?) ORDER BY attempt_no DESC LIMIT 1",
                (intent_key, intent_key),
            ).fetchone()
            if (existing and str(existing["status"]) == "FAILED"
                    and str(existing["error_code"] or "") == "PRE_BROKER_REJECTED"):
                attempt_no = int(existing["attempt_no"] or 1) + 1
                retry_of = str(existing["intent_id"])
                intent_id = hashlib.sha256(f"{intent_key}:attempt:{attempt_no}".encode()).hexdigest()
                existing = None
            if existing:
                if hmac.compare_digest(str(existing["payload_hash"]), payload_hash):
                    result = self._auto_intent_dict(existing)
                else:
                    self._create_execution_hold_tx(
                        con, "INTENT", str(existing["intent_id"]),
                        "AUTO_INTENT_PAYLOAD_CONFLICT", "auto_executor",
                    )
                    con.execute("DELETE FROM broker_sync_proof")
                    self._event_tx(
                        con, "auto_intent_payload_conflict", "auto_executor", "critical",
                        "Deterministic auto intent key received a conflicting payload",
                        {"intent_id_hash": str(existing["intent_id"])},
                    )
                    con.commit()
                    conflict = True
            else:
                state = con.execute("SELECT * FROM strategy_state WHERE id=1").fetchone()
                if state["lifecycle"] != "ACTIVE":
                    raise ControlRejected(
                        f"Trading system is {state['lifecycle']}: "
                        f"{state['freeze_reason'] or 'not armed'}"
                    )
                if int(state["config_version"]) != config_version or str(state["strategy_id"]) != strategy_id:
                    raise ControlRejected("Auto intent does not match active strategy configuration")
                if float(state["strategy_equity"]) <= float(state["loss_floor"]):
                    self._create_execution_hold_tx(
                        con, "SYSTEM", "*", "STRATEGY_EQUITY_AT_OR_BELOW_7500", "risk_engine"
                    )
                    con.commit()
                    raise ControlRejected("Strategy equity reached the immutable USD 7,500 loss floor")
                if con.execute(
                    "SELECT 1 FROM execution_holds WHERE resolved_at IS NULL AND "
                    "((scope_type='SYSTEM' AND scope_key='*') OR "
                    "(scope_type='SYMBOL' AND scope_key=?)) LIMIT 1", (symbol,)
                ).fetchone():
                    raise ControlRejected("An execution hold blocks this automatic intent")
                if con.execute(
                    "SELECT 1 FROM auto_order_intents "
                    "WHERE status NOT IN ('FILLED','CANCELLED','FAILED') LIMIT 1"
                ).fetchone():
                    raise ControlRejected("An unresolved auto intent globally blocks creation")
                config_row = con.execute(
                    "SELECT config_json FROM strategy_config WHERE active=1 AND version=?",
                    (config_version,),
                ).fetchone()
                if not config_row:
                    raise ControlRejected("Active strategy configuration is missing")
                config = json.loads(config_row["config_json"])
                notional = order_qty * limit_price
                if notional > float(config["max_order_notional"]) + 1e-6:
                    raise ControlRejected("Auto intent exceeds configured maximum order notional")
                if daily_order_notional + notional > float(config["max_daily_order_notional"]) + 1e-6:
                    raise ControlRejected("Auto intent exceeds configured daily order notional")
                reservations = con.execute(
                    "SELECT COALESCE(SUM(reserved_notional),0) AS buy_notional "
                    "FROM auto_order_intents WHERE status NOT IN ('FILLED','CANCELLED','FAILED')"
                ).fetchone()
                auto_buy = float(reservations["buy_notional"])
                if side == "BUY":
                    broker_buy = max(float(state["reserved_buy_notional"]),
                                     broker_pending_buy_notional)
                    projected = float(state["owned_market_value"]) + broker_buy + auto_buy + notional
                    if projected > float(state["exposure_cap"]) + 1e-6:
                        raise ControlRejected("Projected strategy exposure exceeds immutable USD 10,000 cap")
                    if broker_buy + auto_buy + notional > float(state["allocated_cash"]) + 1e-6:
                        raise ControlRejected("Auto intent exceeds strategy sub-ledger cash")
                    reserved_notional, reserved_sell_qty = notional, 0.0
                else:
                    row = con.execute(
                        "SELECT quantity FROM owned_positions WHERE symbol=?", (symbol,)
                    ).fetchone()
                    owned = float(row["quantity"]) if row else 0.0
                    row = con.execute(
                        "SELECT COALESCE(SUM(reserved_sell_qty),0) AS qty "
                        "FROM auto_order_intents WHERE symbol=? "
                        "AND status NOT IN ('FILLED','CANCELLED','FAILED')", (symbol,)
                    ).fetchone()
                    if broker_pending_sell_qty + float(row["qty"]) + order_qty > owned + 1e-9:
                        raise ControlRejected("Cannot reserve more than strategy-owned shares")
                    reserved_notional, reserved_sell_qty = 0.0, order_qty
                now = utcnow()
                con.execute(
                    """INSERT INTO auto_order_intents
                    (intent_id,payload_hash,strategy_id,config_version,signal_batch_id,
                     signal_source_date,factor_set_hash,symbol,side,purpose,target_qty,
                     order_qty,limit_price,status,preview_id,reserved_notional,
                     reserved_sell_qty,created_at,updated_at,error_code,intent_key,attempt_no,retry_of)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'RESERVED',NULL,?,?,?,?,NULL,?,?,?)""",
                    (intent_id, payload_hash, strategy_id, config_version, signal_batch_id,
                     signal_source_date, factor_set_hash, symbol, side, purpose, target_qty,
                     order_qty, limit_price, reserved_notional, reserved_sell_qty, now, now,
                     intent_key, attempt_no, retry_of),
                )
                result = self._auto_intent_dict(con.execute(
                    "SELECT * FROM auto_order_intents WHERE intent_id=?", (intent_id,)
                ).fetchone())
        if conflict:
            raise ControlRejected("Deterministic auto intent payload conflict; execution held")
        if result is None:
            raise ControlRejected("Auto intent creation failed")
        return result

    _AUTO_TRANSITIONS = {
        "PLANNED": frozenset({"RESERVED", "CANCELLED", "FAILED"}),
        "RESERVED": frozenset({"DISPATCHING", "CANCELLED", "FAILED"}),
        "DISPATCHING": frozenset({"ACKED", "PARTIAL", "FILLED", "CANCELLED", "FAILED", "UNKNOWN"}),
        "UNKNOWN": frozenset({"ACKED", "PARTIAL", "FILLED", "CANCELLED", "FAILED"}),
        "ACKED": frozenset({"PARTIAL", "FILLED", "CANCELLED", "FAILED", "UNKNOWN"}),
        "PARTIAL": frozenset({"FILLED", "CANCELLED", "FAILED", "UNKNOWN"}),
        "FILLED": frozenset(), "CANCELLED": frozenset(), "FAILED": frozenset(),
    }

    def _mark_auto_intent(self, intent_id: str, status: str, *,
                          preview_id: str | None = None,
                          error_code: str | None = None) -> dict[str, Any]:
        status = str(status).upper()
        if status not in AUTO_INTENT_STATUSES:
            raise ControlRejected("Invalid auto intent status")
        if error_code is not None and not _INTENT_ERROR_CODE.fullmatch(str(error_code)):
            raise ControlRejected("Invalid error_code; use a secret-free symbolic code")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM auto_order_intents WHERE intent_id=?", (str(intent_id),)
            ).fetchone()
            if not row:
                raise ControlRejected("Auto intent not found")
            current = str(row["status"])
            if status not in self._AUTO_TRANSITIONS[current]:
                raise ControlRejected(f"Illegal auto intent transition {current}->{status}")
            if status == "DISPATCHING":
                preview_id = str(preview_id or "").strip()
                if not preview_id or len(preview_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", preview_id):
                    raise ControlRejected("A valid secret-free preview_id is required")
            elif preview_id is not None:
                raise ControlRejected("preview_id can only be bound when dispatching")
            release = status in {"FILLED", "CANCELLED", "FAILED"}
            con.execute(
                "UPDATE auto_order_intents SET status=?,preview_id=COALESCE(?,preview_id),"
                "error_code=?,reserved_notional=CASE WHEN ? THEN 0 ELSE reserved_notional END,"
                "reserved_sell_qty=CASE WHEN ? THEN 0 ELSE reserved_sell_qty END,"
                "updated_at=? WHERE intent_id=?",
                (status, preview_id, error_code, release, release, utcnow(), str(intent_id)),
            )
            updated = con.execute(
                "SELECT * FROM auto_order_intents WHERE intent_id=?", (str(intent_id),)
            ).fetchone()
        return self._auto_intent_dict(updated)

    def handoff_auto_intent_reservation(self, intent_id: str) -> dict[str, Any]:
        """Release local reservation only after caller proves Broker order visibility."""
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM auto_order_intents WHERE intent_id=?", (str(intent_id),)
            ).fetchone()
            if not row or str(row["status"]) not in {"ACKED", "PARTIAL"}:
                raise ControlRejected("Broker reservation handoff requires ACKED or PARTIAL intent")
            con.execute(
                "UPDATE auto_order_intents SET reserved_notional=0,reserved_sell_qty=0,"
                "updated_at=? WHERE intent_id=?", (utcnow(), str(intent_id)),
            )
            updated = con.execute(
                "SELECT * FROM auto_order_intents WHERE intent_id=?", (str(intent_id),)
            ).fetchone()
        return self._auto_intent_dict(updated)

    def mark_auto_intent_dispatching(self, intent_id: str, preview_id: str) -> dict[str, Any]:
        return self._mark_auto_intent(intent_id, "DISPATCHING", preview_id=preview_id)

    def mark_auto_intent_acked(self, intent_id: str) -> dict[str, Any]:
        return self._mark_auto_intent(intent_id, "ACKED")

    def mark_auto_intent_partial(self, intent_id: str) -> dict[str, Any]:
        return self._mark_auto_intent(intent_id, "PARTIAL")

    def mark_auto_intent_filled(self, intent_id: str) -> dict[str, Any]:
        return self._mark_auto_intent(intent_id, "FILLED")

    def mark_auto_intent_cancelled(self, intent_id: str,
                                   error_code: str | None = None) -> dict[str, Any]:
        return self._mark_auto_intent(intent_id, "CANCELLED", error_code=error_code)

    def mark_auto_intent_failed(self, intent_id: str,
                                error_code: str) -> dict[str, Any]:
        return self._mark_auto_intent(intent_id, "FAILED", error_code=error_code)

    def mark_auto_intent_unknown(self, intent_id: str,
                                 error_code: str) -> dict[str, Any]:
        return self._mark_auto_intent(intent_id, "UNKNOWN", error_code=error_code)

    def add_external_symbols(self, symbols: list[str], source: str) -> int:
        normalized = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        if not normalized:
            return 0
        with self.connect() as con:
            before = con.total_changes
            now = utcnow()
            for symbol in normalized:
                con.execute("INSERT OR IGNORE INTO external_symbol_denylist VALUES(?,?,?)",
                            (symbol, now, str(source)))
            inserted = con.total_changes - before
            if inserted:
                self._invalidate_runtime_tx(con, "external_symbol_denylist_changed")
            return inserted

    def denied_symbols(self) -> set[str]:
        with self.connect() as con:
            rows = con.execute("SELECT symbol FROM external_symbol_denylist ORDER BY symbol").fetchall()
        return {str(row[0]) for row in rows}

    def denylist_hash(self) -> str:
        payload = "\n".join(sorted(self.denied_symbols())).encode()
        return hashlib.sha256(payload).hexdigest()

    def record_manual_conflicts(self, symbols: list[str], reason: str) -> int:
        normalized = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        if not normalized:
            return 0
        with self.connect() as con:
            before = con.total_changes
            now = utcnow()
            for symbol in normalized:
                con.execute("INSERT OR IGNORE INTO manual_symbol_conflicts VALUES(?,?,?)",
                            (symbol, now, str(reason)))
            inserted = con.total_changes - before
            if inserted:
                self._invalidate_runtime_tx(con, "manual_symbol_conflict_added")
            return inserted

    def manual_conflict_symbols(self) -> set[str]:
        with self.connect() as con:
            rows = con.execute("SELECT symbol FROM manual_symbol_conflicts ORDER BY symbol").fetchall()
        return {str(row[0]) for row in rows}

    def manual_conflict_hash(self) -> str:
        payload = "\n".join(sorted(self.manual_conflict_symbols())).encode()
        return hashlib.sha256(payload).hexdigest()

    def observe_runtime_fingerprint(self, fingerprint: str) -> int:
        if not fingerprint:
            raise ControlRejected("Runtime fingerprint is required")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT generation,fingerprint FROM control_runtime WHERE id=1").fetchone()
            now = utcnow()
            if not row:
                con.execute("INSERT INTO control_runtime VALUES(1,1,?,?)", (fingerprint, now))
                return 1
            if hmac.compare_digest(str(row["fingerprint"]), fingerprint):
                return int(row["generation"])
            if str(row["fingerprint"]).startswith("pending:"):
                con.execute("UPDATE control_runtime SET fingerprint=?,changed_at=? WHERE id=1",
                            (fingerprint, now))
                return int(row["generation"])
            generation = int(row["generation"]) + 1
            con.execute("UPDATE control_runtime SET generation=?,fingerprint=?,changed_at=? WHERE id=1",
                        (generation, fingerprint, now))
            con.execute("DELETE FROM broker_sync_proof")
            self._create_execution_hold_tx(con, "SYSTEM", "*",
                                           "RUNTIME_IDENTITY_CHANGED_REQUIRES_SYNC", "runtime")
            self._event_tx(con, "control_generation_changed", "runtime", "critical",
                           "Account isolation runtime identity changed; execution held",
                           {"generation": generation})
            return generation

    def current_control_generation(self) -> int:
        with self.connect() as con:
            row = con.execute("SELECT generation FROM control_runtime WHERE id=1").fetchone()
        return int(row[0]) if row else 0

    def record_broker_sync_proof(self, fingerprint: str, synced_at: str) -> None:
        if not fingerprint or not synced_at:
            raise ControlRejected("Broker sync proof requires fingerprint and timestamp")
        with self.connect() as con:
            runtime = con.execute("SELECT generation,fingerprint FROM control_runtime WHERE id=1").fetchone()
            if not runtime or not hmac.compare_digest(str(runtime["fingerprint"]), fingerprint):
                raise ControlRejected("Broker sync proof does not match current control generation")
            con.execute("INSERT INTO broker_sync_proof "
                        "(id,fingerprint,synced_at,control_generation) VALUES(1,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint,"
                        "synced_at=excluded.synced_at,control_generation=excluded.control_generation",
                        (fingerprint, synced_at, int(runtime["generation"])))
            now = utcnow()
            for reason, source in (("CONTROL_GENERATION_CHANGED_REQUIRES_SYNC", "control"),
                                   ("RUNTIME_IDENTITY_CHANGED_REQUIRES_SYNC", "runtime")):
                con.execute("UPDATE execution_holds SET resolved_at=?,resolved_by='moomoo_reconciler',"
                            "resolution_reason='matching broker sync proof recorded' "
                            "WHERE scope_type='SYSTEM' AND scope_key='*' AND reason_code=? "
                            "AND source=? AND resolved_at IS NULL", (now, reason, source))

    def broker_sync_proof_matches(self, fingerprint: str) -> bool:
        with self.connect() as con:
            proof = con.execute("SELECT fingerprint,synced_at,control_generation "
                                "FROM broker_sync_proof WHERE id=1").fetchone()
            runtime = con.execute("SELECT generation,fingerprint FROM control_runtime WHERE id=1").fetchone()
            state = con.execute("SELECT last_sync_at FROM strategy_state WHERE id=1").fetchone()
        return bool(proof and runtime and state and state["last_sync_at"]
                    and int(proof["control_generation"]) == int(runtime["generation"])
                    and hmac.compare_digest(str(runtime["fingerprint"]), fingerprint)
                    and hmac.compare_digest(str(proof["fingerprint"]), fingerprint)
                    and str(proof["synced_at"]) == str(state["last_sync_at"]))

    @contextmanager
    def final_dispatch_guard(self, config_version: int, *, symbol: str,
                             auto_intent_id: str | None = None,
                             preview_id: str | None = None):
        """Hold the strategy DB write lock across the final Broker mutation."""
        dispatch_symbol = str(symbol or "").strip().upper()
        if not dispatch_symbol:
            raise ControlRejected("Final dispatch requires an explicit symbol")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            state = con.execute(
                "SELECT lifecycle,config_version FROM strategy_state WHERE id=1"
            ).fetchone()
            if (not state or state["lifecycle"] != "ACTIVE"
                    or int(state["config_version"]) != int(config_version)):
                raise ControlRejected("Strategy lifecycle or configuration changed at dispatch")
            if auto_intent_id is not None:
                intent = con.execute(
                    "SELECT config_version,status,preview_id,symbol FROM auto_order_intents WHERE intent_id=?",
                    (str(auto_intent_id),),
                ).fetchone()
                if (not intent or int(intent["config_version"]) != int(config_version)
                        or intent["status"] != "DISPATCHING"
                        or str(intent["preview_id"] or "") != str(preview_id or "")
                        or str(intent["symbol"]).strip().upper() != dispatch_symbol):
                    raise ControlRejected("Automatic intent changed at dispatch")
            hold = con.execute(
                "SELECT reason_code FROM execution_holds WHERE resolved_at IS NULL AND "
                "((scope_type='SYSTEM' AND scope_key='*') OR "
                "(scope_type='SYMBOL' AND scope_key=?) OR "
                "(scope_type='INTENT' AND scope_key=?)) LIMIT 1",
                (dispatch_symbol or "", str(auto_intent_id or "")),
            ).fetchone()
            if hold:
                raise ControlRejected("Execution hold blocks final dispatch: " + str(hold["reason_code"]))
            unresolved = con.execute(
                "SELECT intent_id FROM auto_order_intents WHERE "
                "status NOT IN ('FILLED','CANCELLED','FAILED') AND intent_id<>? LIMIT 1",
                (str(auto_intent_id or ""),),
            ).fetchone()
            if unresolved:
                raise ControlRejected("An unresolved auto intent globally blocks final dispatch")
            yield

    def pretrade_guard(self, side: str, symbol: str, quantity: float,
                       limit_price: float, pending_buy_notional: float = 0.0,
                       pending_sell_qty: float = 0.0) -> RiskSnapshot:
        state = self.snapshot()
        if state.lifecycle != "ACTIVE":
            raise ControlRejected(f"Trading system is {state.lifecycle}: {state.freeze_reason or 'not armed'}")
        holds = self.applicable_execution_holds(symbol=symbol)
        if holds:
            raise ControlRejected("Execution is held: " + str(holds[0]["reason_code"]))
        if state.strategy_equity <= state.loss_floor:
            self.create_execution_hold("SYSTEM", "*", "STRATEGY_EQUITY_AT_OR_BELOW_7500", "risk_engine")
            raise ControlRejected("Strategy equity reached the immutable USD 7,500 loss floor")
        notional = _finite(quantity, "quantity") * _finite(limit_price, "limit_price")
        if side.upper() == "BUY":
            projected = state.owned_market_value + state.reserved_buy_notional + pending_buy_notional + notional
            if projected > state.exposure_cap + 1e-6:
                raise ControlRejected("Projected strategy exposure exceeds immutable USD 10,000 cap")
            if notional > state.allocated_cash + 1e-6:
                raise ControlRejected("Order exceeds strategy sub-ledger cash")
        elif side.upper() == "SELL":
            available = self.owned_quantity(symbol) - max(0.0, pending_sell_qty)
            if quantity > available + 1e-9:
                raise ControlRejected("Cannot sell shares not acquired by this strategy")
        else:
            raise ControlRejected("Only BUY and SELL are supported")
        return state

    def apply_fill(self, external_reference: str, symbol: str, side: str,
                   quantity: float, price: float, fee: float = 0.0) -> bool:
        """Apply only a previously verified module-tagged Moomoo fill."""
        reference_hash = hashlib.sha256(str(external_reference).encode()).hexdigest()
        symbol = str(symbol).strip().upper()
        side = str(side).strip().upper()
        quantity, price, fee = (_finite(quantity, "quantity"), _finite(price, "price"), _finite(fee, "fee"))
        if side not in {"BUY", "SELL"} or quantity <= 0 or price < 0 or fee < 0:
            raise ControlRejected("Invalid fill")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT symbol,side,quantity,price,fee FROM applied_fills WHERE fill_hash=?",
                (reference_hash,),
            ).fetchone()
            if existing:
                replay = (symbol, side, quantity, price, fee)
                persisted = (
                    str(existing["symbol"]).strip().upper(),
                    str(existing["side"]).strip().upper(),
                    float(existing["quantity"]), float(existing["price"]), float(existing["fee"]),
                )
                if replay == persisted:
                    return False
                self._latch_fill_conflict_tx(con, symbol, replay, persisted)
                con.commit()
                raise ControlRejected("Existing deal reference has a conflicting replay")
            state = con.execute("SELECT * FROM strategy_state WHERE id=1").fetchone()
            position = con.execute("SELECT * FROM owned_positions WHERE symbol=?", (symbol,)).fetchone()
            old_qty = float(position["quantity"]) if position else 0.0
            old_cost = float(position["average_cost"]) if position else 0.0
            realized = float(position["realized_pnl"]) if position else 0.0
            cash = float(state["allocated_cash"])
            if side == "BUY":
                cost = quantity * price + fee
                if cost > cash + 1e-6:
                    self._event_tx(con, "fill_rejected", "reconciler", "critical",
                                   "Moomoo fill exceeds strategy sub-ledger cash", {"symbol": symbol})
                    raise ControlRejected("Fill exceeds strategy sub-ledger cash")
                new_qty = old_qty + quantity
                new_cost = ((old_qty * old_cost) + quantity * price + fee) / new_qty
                cash -= cost
            else:
                if quantity > old_qty + 1e-9:
                    self._event_tx(con, "ownership_breach", "reconciler", "critical",
                                   "Moomoo sell fill exceeds strategy-owned quantity", {"symbol": symbol})
                    raise ControlRejected("Sell fill exceeds strategy-owned quantity")
                new_qty = max(0.0, old_qty - quantity)
                realized += quantity * (price - old_cost) - fee
                new_cost = old_cost if new_qty else 0.0
                cash += quantity * price - fee
            market_price = price
            market_value = new_qty * market_price
            con.execute("""INSERT INTO owned_positions
                (symbol,quantity,average_cost,market_price,market_value,realized_pnl,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET
                quantity=excluded.quantity,average_cost=excluded.average_cost,
                market_price=excluded.market_price,market_value=excluded.market_value,
                realized_pnl=excluded.realized_pnl,updated_at=excluded.updated_at""",
                (symbol, new_qty, new_cost, market_price, market_value, realized, utcnow()))
            con.execute(
                "INSERT INTO applied_fills "
                "(fill_hash,symbol,side,quantity,price,fee,applied_at,fee_is_stable,order_hash) "
                "VALUES(?,?,?,?,?,?,?,1,NULL)",
                (reference_hash, symbol, side, quantity, price, fee, utcnow()),
            )
            con.execute("UPDATE strategy_state SET allocated_cash=?,updated_at=? WHERE id=1",
                        (cash, utcnow()))
            self._event_tx(con, "fill_applied", "moomoo_reconciler", "info",
                           f"Strategy {side.lower()} fill reconciled", {"symbol": symbol, "quantity": quantity})
        self.mark_to_market({symbol: price}, sync_complete=False)
        return True

    def apply_fill_batch(self, fills: list[dict[str, Any]],
                         broker_quantities: dict[str, float], prices: dict[str, float],
                         reserved_buy_notional: float, sync_fingerprint: str,
                         allow_external_overlap: bool = False, *,
                         account_isolation_mode: str,
                         quantity_observed_at: str,
                         order_fee_observations: list[dict[str, Any]] | None = None) -> int:
        """Atomically apply fills, marks, reservations, risk state, and sync proof."""
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            state = con.execute("SELECT * FROM strategy_state WHERE id=1").fetchone()
            position_rows = con.execute("SELECT * FROM owned_positions").fetchall()
            positions = {str(row["symbol"]): dict(row) for row in position_rows}
            cash = float(state["allocated_cash"])
            reserved_buy_notional = _finite(reserved_buy_notional, "reserved_buy_notional")
            if reserved_buy_notional < 0 or not sync_fingerprint:
                raise ControlRejected("Invalid reconciliation reservation or sync fingerprint")
            staged: list[dict[str, Any]] = []
            stable_fee_bindings: list[dict[str, Any]] = []
            order_hash_bindings: list[tuple[str, str]] = []
            staged_by_hash: dict[str, tuple[str, str, float, float, float]] = {}
            replayed_fees_by_order: dict[str, float] = {}
            new_fees_by_order: dict[str, float] = {}

            for fill in fills:
                reference_hash = hashlib.sha256(str(fill["external_reference"]).encode()).hexdigest()
                symbol = str(fill["symbol"]).strip().upper()
                side = str(fill["side"]).strip().upper()
                quantity = _finite(fill["quantity"], "quantity")
                price = _finite(fill["price"], "price")
                fee = _finite(fill.get("fee", 0.0), "fee")
                fee_is_stable = bool(fill.get("fee_is_stable", False))
                external_order_reference = str(fill.get("external_order_reference") or "")
                order_hash = (hashlib.sha256(external_order_reference.encode()).hexdigest()
                              if external_order_reference else None)
                if side not in {"BUY", "SELL"} or quantity <= 0 or price <= 0 or fee < 0:
                    raise ControlRejected("Invalid fill in reconciliation batch")
                replay = (symbol, side, quantity, price, fee)
                existing = con.execute(
                    "SELECT symbol,side,quantity,price,fee,fee_is_stable,order_hash "
                    "FROM applied_fills WHERE fill_hash=?",
                    (reference_hash,),
                ).fetchone()
                if existing:
                    persisted = (
                        str(existing["symbol"]).strip().upper(),
                        str(existing["side"]).strip().upper(),
                        float(existing["quantity"]), float(existing["price"]), float(existing["fee"]),
                    )
                    same_fill = replay[:4] == persisted[:4]
                    stable_fee_conflict = bool(
                        existing["fee_is_stable"] and fee_is_stable and fee != persisted[4]
                    )
                    persisted_order_hash = existing["order_hash"]
                    if (persisted_order_hash is not None and order_hash is not None
                            and not hmac.compare_digest(str(persisted_order_hash), order_hash)):
                        self._latch_fill_conflict_tx(
                            con, symbol, replay, persisted, ("order_ownership",)
                        )
                        con.commit()
                        raise ControlRejected(
                            "Existing deal reference belongs to a different order"
                        )
                    if same_fill and not stable_fee_conflict:
                        if persisted_order_hash is None and order_hash is not None:
                            order_hash_bindings.append((reference_hash, order_hash))
                        late_fee_credit = 0.0
                        if fee_is_stable and not existing["fee_is_stable"]:
                            if persisted[4] not in {0.0, fee}:
                                self._latch_fill_conflict_tx(con, symbol, replay, persisted)
                                con.commit()
                                raise ControlRejected(
                                    "Existing provisional deal fee conflicts with stable fee"
                                )
                            late_fee_credit = fee - persisted[4]
                            stable_fee_bindings.append({
                                "fill_hash": reference_hash, "symbol": symbol,
                                "fee": fee, "fee_delta": late_fee_credit,
                                "order_hash": order_hash or persisted_order_hash,
                            })
                        if order_hash:
                            replayed_fees_by_order[order_hash] = (
                                replayed_fees_by_order.get(order_hash, 0.0) + persisted[4]
                            )
                            if late_fee_credit:
                                new_fees_by_order[order_hash] = (
                                    new_fees_by_order.get(order_hash, 0.0) + late_fee_credit
                                )
                        continue
                    self._latch_fill_conflict_tx(con, symbol, replay, persisted)
                    con.commit()
                    raise ControlRejected("Existing deal reference has a conflicting replay")
                prior_staged = staged_by_hash.get(reference_hash)
                if prior_staged is not None:
                    if replay == prior_staged:
                        continue
                    self._latch_fill_conflict_tx(con, symbol, replay, prior_staged)
                    con.commit()
                    raise ControlRejected("Duplicate deal reference has a conflicting replay")
                staged_by_hash[reference_hash] = replay

                position = positions.get(symbol, {
                    "symbol": symbol, "quantity": 0.0, "average_cost": 0.0,
                    "market_price": 0.0, "market_value": 0.0, "realized_pnl": 0.0,
                })
                old_qty = float(position["quantity"])
                old_cost = float(position["average_cost"])
                realized = float(position["realized_pnl"])
                if side == "BUY":
                    cost = quantity * price + fee
                    if cost > cash + 1e-6:
                        raise ControlRejected("Fill batch exceeds strategy sub-ledger cash")
                    new_qty = old_qty + quantity
                    new_cost = ((old_qty * old_cost) + quantity * price + fee) / new_qty
                    cash -= cost
                else:
                    if quantity > old_qty + 1e-9:
                        raise ControlRejected("Sell fill batch exceeds strategy-owned quantity")
                    new_qty = max(0.0, old_qty - quantity)
                    realized += quantity * (price - old_cost) - fee
                    new_cost = old_cost if new_qty else 0.0
                    cash += quantity * price - fee
                positions[symbol] = {
                    **position, "symbol": symbol, "quantity": new_qty,
                    "average_cost": new_cost, "market_price": price,
                    "market_value": new_qty * price, "realized_pnl": realized,
                }
                staged.append({
                    "fill_hash": reference_hash, "symbol": symbol, "side": side,
                    "quantity": quantity, "price": price, "fee": fee,
                    "fee_is_stable": fee_is_stable, "order_hash": order_hash,
                })
                if order_hash:
                    new_fees_by_order[order_hash] = new_fees_by_order.get(order_hash, 0.0) + fee

            for symbol, position in positions.items():
                expected = float(position["quantity"])
                actual = _finite(broker_quantities.get(symbol, 0.0), "broker_quantity")
                mismatch = (actual + 1e-9 < expected if allow_external_overlap
                            else abs(actual - expected) > 1e-9)
                if mismatch:
                    # No staged ledger rows have been written. Keep the original
                    # BEGIN IMMEDIATE lock while atomically latching both state and
                    # the secret-free diagnostic; there is no ACTIVE writer window.
                    now = utcnow()
                    self._create_execution_hold_tx(
                        con, "SYMBOL", symbol, "RECONCILIATION_QUANTITY_MISMATCH",
                        "moomoo_reconciler",
                    )
                    con.execute("DELETE FROM broker_sync_proof")
                    self._event_tx(
                        con, "reconciliation_quantity_mismatch", "moomoo_reconciler", "critical",
                        "Broker quantity differs from staged strategy quantity; batch rolled back",
                        {
                            "symbol": symbol,
                            "expected_quantity": expected,
                            "observed_quantity": actual,
                            "observed_at": str(quantity_observed_at),
                            "observation_stage": "broker_position_snapshot_after_deal_staging",
                            "account_isolation_mode": str(account_isolation_mode),
                        },
                    )
                    con.commit()
                    raise ControlRejected(
                        "Broker quantity differs from staged strategy quantity; batch rolled back"
                    )

            staged_fee_accounts: list[dict[str, Any]] = []
            staged_fee_adjustments: list[dict[str, Any]] = []
            observed_order_hashes: set[str] = set()
            fee_cash_delta = 0.0
            for observation in order_fee_observations or []:
                external_order_reference = str(observation.get("external_order_reference") or "")
                if not external_order_reference:
                    raise ControlRejected("Order fee observation requires an external reference")
                order_hash = hashlib.sha256(external_order_reference.encode()).hexdigest()
                symbol = str(observation.get("symbol") or "").strip().upper()
                side = str(observation.get("side") or "").strip().upper()
                cumulative_fee = _finite(observation.get("cumulative_fee"), "cumulative_fee")
                finalized = bool(observation.get("finalized", False))
                if (order_hash in observed_order_hashes or not symbol
                        or side not in {"BUY", "SELL"} or cumulative_fee < 0):
                    raise ControlRejected("Invalid or duplicate order fee observation")
                observed_order_hashes.add(order_hash)
                existing_account = con.execute(
                    "SELECT symbol,side,cumulative_fee,finalized,revision "
                    "FROM order_fee_accounts WHERE order_hash=?", (order_hash,),
                ).fetchone()
                if existing_account and (
                    str(existing_account["symbol"]) != symbol
                    or str(existing_account["side"]) != side
                ):
                    replay = (symbol, side, 0.0, 0.0, cumulative_fee)
                    persisted = (
                        str(existing_account["symbol"]), str(existing_account["side"]),
                        0.0, 0.0, float(existing_account["cumulative_fee"]),
                    )
                    self._latch_fill_conflict_tx(con, symbol, replay, persisted)
                    con.commit()
                    raise ControlRejected("Existing order fee reference has a conflicting replay")
                previous_total = (
                    float(existing_account["cumulative_fee"]) if existing_account else 0.0
                )
                fill_fee_credit = new_fees_by_order.get(order_hash, 0.0)
                if not existing_account:
                    fill_fee_credit += replayed_fees_by_order.get(order_hash, 0.0)
                delta = cumulative_fee - previous_total - fill_fee_credit
                revision = int(existing_account["revision"]) if existing_account else 0
                should_audit = cumulative_fee != previous_total or fill_fee_credit != 0.0
                if should_audit:
                    revision += 1
                    material = "|".join((
                        order_hash, str(revision), format(previous_total, ".17g"),
                        format(cumulative_fee, ".17g"), format(fill_fee_credit, ".17g"),
                        format(delta, ".17g"),
                    ))
                    staged_fee_adjustments.append({
                        "adjustment_hash": hashlib.sha256(material.encode()).hexdigest(),
                        "order_hash": order_hash, "previous_total": previous_total,
                        "new_total": cumulative_fee, "fill_fee_credit": fill_fee_credit,
                        "delta": delta,
                    })
                fee_cash_delta += delta
                staged_fee_accounts.append({
                    "order_hash": order_hash, "symbol": symbol, "side": side,
                    "cumulative_fee": cumulative_fee,
                    "finalized": bool(finalized or (existing_account and existing_account["finalized"])),
                    "revision": revision,
                })
            stable_fee_cash_delta = sum(
                float(binding["fee_delta"]) for binding in stable_fee_bindings
            )
            cash -= fee_cash_delta + stable_fee_cash_delta

            # A stable per-deal fee is part of that fill's original economics.
            # Replaying the affected symbol makes a late fee indistinguishable
            # from one present on first observation, including after partial sells.
            if stable_fee_bindings:
                fee_overrides = {
                    str(binding["fill_hash"]): float(binding["fee"])
                    for binding in stable_fee_bindings
                }
                affected_symbols = {
                    str(binding["symbol"]) for binding in stable_fee_bindings
                }
                persisted_fills = con.execute(
                    "SELECT rowid,fill_hash,symbol,side,quantity,price,fee "
                    "FROM applied_fills ORDER BY rowid"
                ).fetchall()
                replay_fills = [dict(row) for row in persisted_fills]
                replay_fills.extend(staged)
                for symbol in affected_symbols:
                    quantity = average_cost = realized = 0.0
                    for fill in replay_fills:
                        if str(fill["symbol"]) != symbol:
                            continue
                        fill_quantity = float(fill["quantity"])
                        fill_price = float(fill["price"])
                        fill_fee = fee_overrides.get(
                            str(fill["fill_hash"]), float(fill["fee"])
                        )
                        if str(fill["side"]) == "BUY":
                            new_quantity = quantity + fill_quantity
                            average_cost = (
                                quantity * average_cost + fill_quantity * fill_price + fill_fee
                            ) / new_quantity
                            quantity = new_quantity
                        else:
                            if fill_quantity > quantity + 1e-9:
                                raise ControlRejected(
                                    "Stable fee replay exceeds strategy-owned quantity"
                                )
                            realized += fill_quantity * (fill_price - average_cost) - fill_fee
                            quantity = max(0.0, quantity - fill_quantity)
                            if quantity == 0:
                                average_cost = 0.0
                    expected_quantity = float(positions[symbol]["quantity"])
                    if abs(quantity - expected_quantity) > 1e-9:
                        raise ControlRejected(
                            "Stable fee replay disagrees with strategy-owned quantity"
                        )
                    positions[symbol]["average_cost"] = average_cost
                    positions[symbol]["realized_pnl"] = realized

            market_value = unrealized = realized_total = 0.0
            for symbol, position in positions.items():
                quantity = float(position["quantity"])
                if quantity > 0:
                    if symbol not in prices:
                        raise ControlRejected("Every staged strategy position requires a fresh Moomoo price")
                    market_price = _finite(prices[symbol], "market_price")
                    if market_price <= 0:
                        raise ControlRejected("Invalid Moomoo market price in reconciliation batch")
                    position["market_price"] = market_price
                    position["market_value"] = quantity * market_price
                    market_value += float(position["market_value"])
                    unrealized += quantity * (market_price - float(position["average_cost"]))
                else:
                    position["market_value"] = 0.0
                realized_total += float(position["realized_pnl"])
            prior_fee_adjustments = con.execute(
                "SELECT COALESCE(SUM(delta),0) FROM order_fee_adjustments"
            ).fetchone()[0]
            realized_total -= float(prior_fee_adjustments) + fee_cash_delta
            equity = cash + market_value
            lifecycle, reason = state["lifecycle"], state["freeze_reason"]
            required_sync_after = state["required_sync_after"]
            breach = None
            if equity <= LOSS_FLOOR:
                breach = "strategy_equity_at_or_below_7500"
            elif market_value + reserved_buy_notional > EXPOSURE_CAP + 1e-6:
                breach = "strategy_exposure_above_10000"
            if breach:
                self._create_execution_hold_tx(con, "SYSTEM", "*", breach.upper(), "risk_engine")

            now = utcnow()
            for position in positions.values():
                con.execute("""INSERT INTO owned_positions
                    (symbol,quantity,average_cost,market_price,market_value,realized_pnl,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET
                    quantity=excluded.quantity,average_cost=excluded.average_cost,
                    market_price=excluded.market_price,market_value=excluded.market_value,
                    realized_pnl=excluded.realized_pnl,updated_at=excluded.updated_at""",
                            (position["symbol"], position["quantity"], position["average_cost"],
                             position["market_price"], position["market_value"],
                             position["realized_pnl"], now))
            for fill in staged:
                con.execute(
                    "INSERT INTO applied_fills "
                    "(fill_hash,symbol,side,quantity,price,fee,applied_at,fee_is_stable,order_hash) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (fill["fill_hash"], fill["symbol"], fill["side"], fill["quantity"],
                     fill["price"], fill["fee"], now, int(fill["fee_is_stable"]),
                     fill["order_hash"]),
                )
                self._event_tx(con, "fill_applied", "moomoo_reconciler", "info",
                               f"Strategy {fill['side'].lower()} fill reconciled",
                               {"symbol": fill["symbol"], "quantity": fill["quantity"]})
            for fill_hash, bound_order_hash in order_hash_bindings:
                con.execute(
                    "UPDATE applied_fills SET order_hash=? "
                    "WHERE fill_hash=? AND order_hash IS NULL",
                    (bound_order_hash, fill_hash),
                )
            for binding in stable_fee_bindings:
                con.execute(
                    "UPDATE applied_fills SET fee=?,fee_is_stable=1,"
                    "order_hash=COALESCE(order_hash,?) "
                    "WHERE fill_hash=? AND fee_is_stable=0",
                    (binding["fee"], binding["order_hash"], binding["fill_hash"]),
                )
            for account in staged_fee_accounts:
                con.execute(
                    """INSERT INTO order_fee_accounts
                    (order_hash,symbol,side,cumulative_fee,finalized,revision,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(order_hash) DO UPDATE SET
                    symbol=excluded.symbol,side=excluded.side,
                    cumulative_fee=excluded.cumulative_fee,
                    finalized=excluded.finalized,revision=excluded.revision,
                    updated_at=excluded.updated_at""",
                    (account["order_hash"], account["symbol"], account["side"],
                     account["cumulative_fee"], int(account["finalized"]),
                     account["revision"], now),
                )
            for adjustment in staged_fee_adjustments:
                con.execute(
                    """INSERT INTO order_fee_adjustments
                    (adjustment_hash,order_hash,previous_total,new_total,fill_fee_credit,delta,applied_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (adjustment["adjustment_hash"], adjustment["order_hash"],
                     adjustment["previous_total"], adjustment["new_total"],
                     adjustment["fill_fee_credit"], adjustment["delta"], now),
                )
                self._event_tx(
                    con, "order_fee_adjusted", "moomoo_reconciler", "info",
                    "Cumulative Broker order fee was reconciled to the strategy ledger",
                    {"symbol": next(
                        account["symbol"] for account in staged_fee_accounts
                        if account["order_hash"] == adjustment["order_hash"]
                    ), "previous_total": adjustment["previous_total"],
                     "new_total": adjustment["new_total"], "delta": adjustment["delta"]},
                )
            if breach:
                required_sync_after = now
                self._event_tx(con, "risk_limit_breach", "risk_engine", "critical",
                               "Immutable strategy risk limit breached",
                               {"reason": breach, "equity": equity,
                                "market_value": market_value})
            con.execute("""UPDATE strategy_state SET lifecycle=?,freeze_latched=?,freeze_reason=?,
                allocated_cash=?,owned_market_value=?,strategy_equity=?,realized_pnl=?,
                unrealized_pnl=?,reserved_buy_notional=?,last_sync_at=?,updated_at=?,
                required_sync_after=? WHERE id=1""",
                        (lifecycle, 1 if lifecycle != "ACTIVE" else 0, reason, cash,
                         market_value, equity, realized_total, unrealized,
                         reserved_buy_notional, now, now, required_sync_after))
            con.execute("INSERT OR REPLACE INTO strategy_equity VALUES(?,?,?,?,?,?,?)",
                        (now, equity, cash, market_value, realized_total, unrealized, lifecycle))
            runtime = con.execute("SELECT generation,fingerprint FROM control_runtime WHERE id=1").fetchone()
            if not runtime or not hmac.compare_digest(str(runtime["fingerprint"]), sync_fingerprint):
                raise ControlRejected("Reconciliation fingerprint changed before atomic commit")
            con.execute("INSERT INTO broker_sync_proof "
                        "(id,fingerprint,synced_at,control_generation) VALUES(1,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint,"
                        "synced_at=excluded.synced_at,control_generation=excluded.control_generation",
                        (sync_fingerprint, now, int(runtime["generation"])))
            for hold_reason, hold_source in (
                ("CONTROL_GENERATION_CHANGED_REQUIRES_SYNC", "control"),
                ("RUNTIME_IDENTITY_CHANGED_REQUIRES_SYNC", "runtime"),
                ("FIVE_MINUTE_RECONCILIATION_FAILED", "moomoo_sync"),
            ):
                con.execute("UPDATE execution_holds SET resolved_at=?,resolved_by='moomoo_reconciler',"
                            "resolution_reason='successful complete reconciliation' "
                            "WHERE scope_type='SYSTEM' AND scope_key='*' AND reason_code=? "
                            "AND source=? AND resolved_at IS NULL", (now, hold_reason, hold_source))
            self._event_tx(con, "account_sync", "moomoo_reconciler", "info",
                           "Five-minute strategy reconciliation completed",
                           {"equity": equity, "market_value": market_value})
        return len(staged)

    def set_reserved_buy_notional(self, value: float) -> None:
        value = _finite(value, "reserved_buy_notional")
        if value < 0:
            raise ControlRejected("Invalid reserved_buy_notional")
        with self.connect() as con:
            con.execute("UPDATE strategy_state SET reserved_buy_notional=?,updated_at=? WHERE id=1",
                        (value, utcnow()))

    def mark_to_market(self, prices: dict[str, float], sync_complete: bool = True) -> RiskSnapshot:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            positions = con.execute("SELECT * FROM owned_positions WHERE quantity>0").fetchall()
            missing = [row["symbol"] for row in positions if row["symbol"] not in prices]
            if missing:
                now = utcnow()
                self._event_tx(con, "price_missing", "reconciler", "critical",
                               "Owned-position prices missing", {"symbols": missing})
                for symbol in missing:
                    self._create_execution_hold_tx(con, "SYMBOL", symbol,
                                                   "OWNED_PRICE_MISSING", "reconciler")
                con.commit()
                raise ControlRejected("Every strategy-owned position requires a fresh Moomoo price")
            market_value = unrealized = realized = 0.0
            for row in positions:
                price = _finite(prices[row["symbol"]], "market_price")
                if price <= 0:
                    raise ControlRejected("Invalid Moomoo market price")
                value = float(row["quantity"]) * price
                market_value += value
                unrealized += float(row["quantity"]) * (price - float(row["average_cost"]))
                realized += float(row["realized_pnl"])
                con.execute("UPDATE owned_positions SET market_price=?,market_value=?,updated_at=? WHERE symbol=?",
                            (price, value, utcnow(), row["symbol"]))
            realized -= float(con.execute(
                "SELECT COALESCE(SUM(delta),0) FROM order_fee_adjustments"
            ).fetchone()[0])
            state = con.execute("SELECT * FROM strategy_state WHERE id=1").fetchone()
            equity = float(state["allocated_cash"]) + market_value
            lifecycle, reason = state["lifecycle"], state["freeze_reason"]
            required_sync_after = state["required_sync_after"]
            now = utcnow()
            breach = None
            if equity <= LOSS_FLOOR:
                breach = "strategy_equity_at_or_below_7500"
            elif market_value + float(state["reserved_buy_notional"]) > EXPOSURE_CAP + 1e-6:
                breach = "strategy_exposure_above_10000"
            if breach:
                self._create_execution_hold_tx(con, "SYSTEM", "*", breach.upper(), "risk_engine")
                self._event_tx(con, "risk_limit_breach", "risk_engine", "critical",
                               "Immutable strategy risk limit breached", {"reason": breach, "equity": equity,
                                                                         "market_value": market_value})
            con.execute("""UPDATE strategy_state SET lifecycle=?,freeze_latched=?,freeze_reason=?,
                owned_market_value=?,strategy_equity=?,realized_pnl=?,unrealized_pnl=?,
                last_sync_at=?,updated_at=?,required_sync_after=? WHERE id=1""",
                (lifecycle, 1 if lifecycle != "ACTIVE" else 0, reason, market_value, equity,
                 realized, unrealized, now if sync_complete else state["last_sync_at"], now,
                 required_sync_after))
            con.execute("INSERT OR REPLACE INTO strategy_equity VALUES(?,?,?,?,?,?,?)",
                        (now, equity, float(state["allocated_cash"]), market_value,
                         realized, unrealized, lifecycle))
            if sync_complete:
                self._event_tx(con, "account_sync", "moomoo_reconciler", "info",
                               "Five-minute strategy reconciliation completed",
                               {"equity": equity, "market_value": market_value})
        return self.snapshot()

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self.connect() as con:
            rows = con.execute("SELECT * FROM strategy_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw_details = item.pop("details_json", "{}")
            try:
                details = json.loads(raw_details)
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {"unparsed": str(raw_details)}
            result.append(_redact(item | {"details": details}))
        return result

    def equity_history(self, limit: int = 5000) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM strategy_equity ORDER BY ts DESC LIMIT ?",
                               (max(1, min(int(limit), 20_000)),)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def upsert_paper_point(self, series_id: str, ts: str, label: str, candidate_type: str,
                           equity: float, return_pct: float | None = None,
                           account_ref: str | None = None, params: dict[str, Any] | None = None) -> None:
        if candidate_type not in {"account", "parameter"}:
            raise ControlRejected("Invalid paper candidate type")
        with self.connect() as con:
            con.execute("INSERT OR REPLACE INTO paper_series VALUES(?,?,?,?,?,?,?,?)",
                        (series_id, ts, label, candidate_type, account_ref,
                         json.dumps(_redact(params or {}), sort_keys=True),
                         _finite(equity, "equity"), None if return_pct is None else _finite(return_pct, "return_pct")))

    def paper_series(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM paper_series ORDER BY series_id,ts").fetchall()
        return [dict(row) | {"params": json.loads(row["params_json"])} for row in rows]

    def cleanup(self, confirmation: str, actor: str, reason: str,
                broker_orders_clear: bool) -> Path:
        if confirmation != "FREEZE ARCHIVE AND CLEAN STRATEGY":
            raise ControlRejected("Exact cleanup confirmation is required")
        self.freeze("cleanup_requested", "operator_cleanup")
        state = self.snapshot()
        if self.positions() or state.reserved_buy_notional > 1e-9:
            raise ControlRejected("Cleanup requires zero strategy-owned positions and zero active BUY reservation")
        if not broker_orders_clear:
            raise ControlRejected("Cleanup requires a fresh broker proof of zero active module orders")
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.archive_dir, 0o700)
        except OSError:
            pass
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.archive_dir / f"strategy-{stamp}.tar.gz"
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            strategy_backup = Path(tmp) / "live_strategy.db"
            with sqlite3.connect(str(self.path)) as source, sqlite3.connect(str(strategy_backup)) as dest:
                source.backup(dest)
            manifest.write_text(json.dumps({"archived_at": utcnow(), "reason": reason,
                                            "state": state.__dict__}, indent=2, default=str))
            with tarfile.open(target, "w:gz") as archive:
                archive.add(strategy_backup, arcname="live_strategy.db")
                audit_db = self.path.parent / "moomoo_live_audit.db"
                log_dir = self.path.parents[1] / "logs" / "live_account"
                if audit_db.exists():
                    audit_backup = Path(tmp) / "moomoo_live_audit.db"
                    with sqlite3.connect(str(audit_db)) as source, sqlite3.connect(str(audit_backup)) as dest:
                        source.backup(dest)
                    archive.add(audit_backup, arcname="moomoo_live_audit.db")
                if log_dir.exists():
                    archive.add(log_dir, arcname="logs/live_account")
                archive.add(manifest, arcname="manifest.json")
        os.chmod(target, 0o600)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE strategy_config SET active=0 WHERE active=1")
            now = utcnow()
            con.execute("UPDATE strategy_state SET lifecycle='CLEANED',freeze_latched=1,"
                        "freeze_reason='cleaned_no_valid_strategy',strategy_id=NULL,updated_at=?,"
                        "required_sync_after=? WHERE id=1", (now, now))
            self._event_tx(con, "strategy_cleaned", actor, "critical",
                           "Strategy rules and active parameters cleaned after archive",
                           {"archive": target.name, "reason": reason})
        return target

    def health(self) -> dict[str, Any]:
        state = self.snapshot()
        problems = []
        execution = self.execution_status()
        if state.lifecycle != "ACTIVE":
            problems.append(f"manual_freeze:{state.lifecycle}")
        if execution["active_holds"]:
            problems.append("automatic_execution_holds")
        if state.strategy_equity <= state.loss_floor:
            problems.append("loss_floor")
        if state.owned_market_value + state.reserved_buy_notional > state.exposure_cap + 1e-6:
            problems.append("exposure_cap")
        return {"healthy": not problems, "problems": problems, "state": state.__dict__,
                "execution_status": execution,
                "config": self.config() if state.strategy_id else None}
