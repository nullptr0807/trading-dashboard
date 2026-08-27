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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "live_strategy.db"
ARCHIVE_DIR = Path(__file__).resolve().parents[1] / "data" / "live_strategy_archives"
INITIAL_CAPITAL = 10_000.0
EXPOSURE_CAP = 10_000.0
LOSS_FLOOR = 7_500.0

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
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(password|token|secret|credential|authorization|account.?id|order.?id|deal.?id)"
    r"\s*[:=]\s*[^\s,;]+"
)
_LONG_NUMBER = re.compile(r"\b\d{6,}\b")
_OPAQUE_WITH_DIGIT = re.compile(r"\b(?=[A-Za-z0-9_-]{12,}\b)(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+\b")
_INTENT_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
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


def _redact(value: Any) -> Any:
    secret_words = ("password", "token", "secret", "account_id", "order_id", "deal_id", "preview")
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if any(w in str(k).lower() for w in secret_words) else _redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        value = _SECRET_ASSIGNMENT.sub(lambda m: m.group(1) + "=[REDACTED]", value)
        value = _LONG_NUMBER.sub("[REDACTED_ID]", value)
        return _OPAQUE_WITH_DIGIT.sub("[REDACTED_ID]", value)
    return value


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
            con = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=20)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            try:
                yield con
            finally:
                con.close()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=20)
        con.row_factory = sqlite3.Row
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _initialize(self) -> None:
        with self.connect() as con:
            con.executescript("""
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
                    applied_at TEXT NOT NULL
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
            """)
            columns = {row[1] for row in con.execute("PRAGMA table_info(strategy_state)")}
            if "required_sync_after" not in columns:
                con.execute("ALTER TABLE strategy_state ADD COLUMN required_sync_after TEXT")
                con.execute("UPDATE strategy_state SET required_sync_after=updated_at")
            proof_columns = {row[1] for row in con.execute("PRAGMA table_info(broker_sync_proof)")}
            if "control_generation" not in proof_columns:
                con.execute("ALTER TABLE broker_sync_proof ADD COLUMN control_generation INTEGER NOT NULL DEFAULT 0")
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
        state = con.execute("SELECT lifecycle,freeze_reason FROM strategy_state WHERE id=1").fetchone()
        freeze_reason = (str(state["freeze_reason"]) if state and state["lifecycle"] == "FROZEN"
                         and state["freeze_reason"] else "control_generation_changed_requires_sync")
        con.execute("UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
                    "freeze_reason=?,updated_at=?,required_sync_after=? WHERE id=1",
                    (freeze_reason, now, now))
        self._event_tx(con, "control_generation_invalidated", "control", "critical",
                       "Control generation changed; broker sync proof invalidated",
                       {"generation": generation, "reason": reason})
        return generation

    def event(self, event_type: str, source: str, severity: str,
              message: str, details: dict[str, Any] | None = None) -> int:
        with self.connect() as con:
            return self._event_tx(con, event_type, source, severity, message, details or {})

    def snapshot(self) -> RiskSnapshot:
        with self.connect() as con:
            row = con.execute("SELECT * FROM strategy_state WHERE id=1").fetchone()
        if not row:
            raise ControlRejected("Strategy control state is missing")
        return RiskSnapshot(
            lifecycle=row["lifecycle"], frozen=row["lifecycle"] != "ACTIVE",
            freeze_reason=row["freeze_reason"], initial_capital=row["initial_capital"],
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
            con.execute("UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
                        "freeze_reason='config_changed_requires_review',updated_at=?,"
                        "required_sync_after=? WHERE id=1", (now, now))
            self._invalidate_runtime_tx(con, "strategy_config_changed")
            self._event_tx(con, "config_reloaded", "dashboard", "warning",
                           "Strategy parameters hot-reloaded; trading frozen for review",
                           {"version": version, "changed_fields": sorted(patch), "reason": reason})
        return self.config()

    def freeze(self, reason: str, source: str = "dashboard", severity: str = "critical") -> RiskSnapshot:
        reason = str(reason or "").strip() or "manual_freeze"
        now = utcnow()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute(
                "SELECT lifecycle,freeze_reason,required_sync_after FROM strategy_state WHERE id=1"
            ).fetchone()
            required = (current["required_sync_after"] if current
                        and current["lifecycle"] == "FROZEN"
                        and current["freeze_reason"] == reason else now)
            con.execute("UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
                        "freeze_reason=?,updated_at=?,required_sync_after=? WHERE id=1",
                        (reason, now, required))
            self._event_tx(con, "system_frozen", source, severity, "Trading system frozen", {"reason": reason})
        return self.snapshot()

    def unfreeze(self, reason: str, actor: str = "dashboard") -> RiskSnapshot:
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

    def execution_summary(self) -> dict[str, Any]:
        with self.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS total_trades, "
                "COALESCE(SUM(fee),0) AS total_fees, "
                "COALESCE(SUM(quantity*price),0) AS total_notional, "
                "COALESCE(SUM(CASE WHEN side='BUY' THEN 1 ELSE 0 END),0) AS buy_trades, "
                "COALESCE(SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END),0) AS sell_trades, "
                "MIN(applied_at) AS first_trade_at, MAX(applied_at) AS last_trade_at "
                "FROM applied_fills"
            ).fetchone()
        return {
            "total_trades": int(row["total_trades"]),
            "total_fees": float(row["total_fees"]),
            "total_notional": float(row["total_notional"]),
            "buy_trades": int(row["buy_trades"]),
            "sell_trades": int(row["sell_trades"]),
            "first_trade_at": row["first_trade_at"],
            "last_trade_at": row["last_trade_at"],
        }

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
        intent_id = hashlib.sha256(canonical_key.encode()).hexdigest()
        payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        conflict = False
        result: dict[str, Any] | None = None

        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM auto_order_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if existing:
                if hmac.compare_digest(str(existing["payload_hash"]), payload_hash):
                    result = self._auto_intent_dict(existing)
                else:
                    now = utcnow()
                    con.execute(
                        "UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
                        "freeze_reason='auto_intent_payload_conflict',updated_at=?,"
                        "required_sync_after=? WHERE id=1", (now, now)
                    )
                    self._event_tx(
                        con, "auto_intent_payload_conflict", "auto_executor", "critical",
                        "Deterministic auto intent key received a conflicting payload",
                        {"intent_id_hash": intent_id},
                    )
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
                    raise ControlRejected("Strategy equity reached the immutable USD 7,500 loss floor")
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
                     reserved_sell_qty,created_at,updated_at,error_code)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'RESERVED',NULL,?,?,?,?,NULL)""",
                    (intent_id, payload_hash, strategy_id, config_version, signal_batch_id,
                     signal_source_date, factor_set_hash, symbol, side, purpose, target_qty,
                     order_qty, limit_price, reserved_notional, reserved_sell_qty, now, now),
                )
                result = self._auto_intent_dict(con.execute(
                    "SELECT * FROM auto_order_intents WHERE intent_id=?", (intent_id,)
                ).fetchone())
        if conflict:
            raise ControlRejected("Deterministic auto intent payload conflict; trading frozen")
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
            state = con.execute("SELECT lifecycle,freeze_reason FROM strategy_state WHERE id=1").fetchone()
            freeze_reason = (str(state["freeze_reason"]) if state and state["lifecycle"] == "FROZEN"
                             and state["freeze_reason"] else "runtime_identity_changed_requires_sync")
            con.execute("UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
                        "freeze_reason=?,updated_at=?,required_sync_after=? WHERE id=1",
                        (freeze_reason, now, now))
            self._event_tx(con, "control_generation_changed", "runtime", "critical",
                           "Account isolation runtime identity changed; trading frozen",
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
    def final_dispatch_guard(self, config_version: int, *,
                             auto_intent_id: str | None = None,
                             preview_id: str | None = None):
        """Hold the strategy DB write lock across the final Broker mutation."""
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
                    "SELECT config_version,status,preview_id FROM auto_order_intents WHERE intent_id=?",
                    (str(auto_intent_id),),
                ).fetchone()
                if (not intent or int(intent["config_version"]) != int(config_version)
                        or intent["status"] != "DISPATCHING"
                        or str(intent["preview_id"] or "") != str(preview_id or "")):
                    raise ControlRejected("Automatic intent changed at dispatch")
            yield

    def pretrade_guard(self, side: str, symbol: str, quantity: float,
                       limit_price: float, pending_buy_notional: float = 0.0,
                       pending_sell_qty: float = 0.0) -> RiskSnapshot:
        state = self.snapshot()
        if state.lifecycle != "ACTIVE":
            raise ControlRejected(f"Trading system is {state.lifecycle}: {state.freeze_reason or 'not armed'}")
        if state.strategy_equity <= state.loss_floor:
            self.freeze("strategy_equity_at_or_below_7500", "risk_engine")
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
        symbol = str(symbol).upper()
        side = str(side).upper()
        quantity, price, fee = (_finite(quantity, "quantity"), _finite(price, "price"), _finite(fee, "fee"))
        if side not in {"BUY", "SELL"} or quantity <= 0 or price < 0 or fee < 0:
            raise ControlRejected("Invalid fill")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM applied_fills WHERE fill_hash=?", (reference_hash,)).fetchone():
                return False
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
            con.execute("INSERT INTO applied_fills VALUES(?,?,?,?,?,?,?)",
                        (reference_hash, symbol, side, quantity, price, fee, utcnow()))
            con.execute("UPDATE strategy_state SET allocated_cash=?,updated_at=? WHERE id=1",
                        (cash, utcnow()))
            self._event_tx(con, "fill_applied", "moomoo_reconciler", "info",
                           f"Strategy {side.lower()} fill reconciled", {"symbol": symbol, "quantity": quantity})
        self.mark_to_market({symbol: price}, sync_complete=False)
        return True

    def apply_fill_batch(self, fills: list[dict[str, Any]],
                         broker_quantities: dict[str, float], prices: dict[str, float],
                         reserved_buy_notional: float, sync_fingerprint: str,
                         allow_external_overlap: bool = False) -> int:
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

            for fill in fills:
                reference_hash = hashlib.sha256(str(fill["external_reference"]).encode()).hexdigest()
                if con.execute("SELECT 1 FROM applied_fills WHERE fill_hash=?",
                               (reference_hash,)).fetchone():
                    continue
                symbol = str(fill["symbol"]).upper()
                side = str(fill["side"]).upper()
                quantity = _finite(fill["quantity"], "quantity")
                price = _finite(fill["price"], "price")
                fee = _finite(fill.get("fee", 0.0), "fee")
                if side not in {"BUY", "SELL"} or quantity <= 0 or price <= 0 or fee < 0:
                    raise ControlRejected("Invalid fill in reconciliation batch")

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
                })

            for symbol, position in positions.items():
                expected = float(position["quantity"])
                actual = _finite(broker_quantities.get(symbol, 0.0), "broker_quantity")
                mismatch = (actual + 1e-9 < expected if allow_external_overlap
                            else abs(actual - expected) > 1e-9)
                if mismatch:
                    raise ControlRejected(
                        "Broker quantity differs from staged strategy quantity; batch rolled back"
                    )

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
            equity = cash + market_value
            lifecycle, reason = state["lifecycle"], state["freeze_reason"]
            required_sync_after = state["required_sync_after"]
            breach = None
            if equity <= LOSS_FLOOR:
                breach = "strategy_equity_at_or_below_7500"
            elif market_value + reserved_buy_notional > EXPOSURE_CAP + 1e-6:
                breach = "strategy_exposure_above_10000"
            if breach:
                lifecycle, reason = "FROZEN", breach

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
                con.execute("INSERT INTO applied_fills VALUES(?,?,?,?,?,?,?)",
                            (fill["fill_hash"], fill["symbol"], fill["side"],
                             fill["quantity"], fill["price"], fill["fee"], now))
                self._event_tx(con, "fill_applied", "moomoo_reconciler", "info",
                               f"Strategy {fill['side'].lower()} fill reconciled",
                               {"symbol": fill["symbol"], "quantity": fill["quantity"]})
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
                con.execute("UPDATE strategy_state SET lifecycle='FROZEN',freeze_latched=1,"
                            "freeze_reason='owned_price_missing',updated_at=?,required_sync_after=? "
                            "WHERE id=1", (now, now))
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
                lifecycle, reason = "FROZEN", breach
                required_sync_after = now
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
        return [dict(row) | {"details": json.loads(row["details_json"])} for row in rows]

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
        self.freeze("cleanup_requested", actor)
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
        if state.lifecycle != "ACTIVE":
            problems.append(f"lifecycle:{state.lifecycle}")
        if state.strategy_equity <= state.loss_floor:
            problems.append("loss_floor")
        if state.owned_market_value + state.reserved_buy_notional > state.exposure_cap + 1e-6:
            problems.append("exposure_cap")
        return {"healthy": not problems, "problems": problems, "state": state.__dict__,
                "config": self.config() if state.strategy_id else None}
