"""Independent USD 10k live-strategy ledger and fail-closed control plane.

This database contains strategy state only. Broker account identifiers, passwords,
tokens and raw order/deal references are intentionally excluded. External broker
positions are never imported as strategy-owned positions.
"""
from __future__ import annotations

import hashlib
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
    def __init__(self, path: str | Path = DB_PATH, archive_dir: str | Path = ARCHIVE_DIR):
        self.path = Path(path)
        self.archive_dir = Path(archive_dir)
        self._initialize()

    @contextmanager
    def connect(self):
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
            """)
            columns = {row[1] for row in con.execute("PRAGMA table_info(strategy_state)")}
            if "required_sync_after" not in columns:
                con.execute("ALTER TABLE strategy_state ADD COLUMN required_sync_after TEXT")
                con.execute("UPDATE strategy_state SET required_sync_after=updated_at")
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
            con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                        "freeze_reason=NULL,updated_at=? WHERE id=1", (utcnow(),))
            self._event_tx(con, "system_unfrozen", actor, "critical", "Trading system unfrozen", {"reason": reason})
        return self.snapshot()

    def positions(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM owned_positions WHERE quantity>0 ORDER BY market_value DESC").fetchall()
        return [dict(row) for row in rows]

    def owned_quantity(self, symbol: str) -> float:
        with self.connect() as con:
            row = con.execute("SELECT quantity FROM owned_positions WHERE symbol=?", (symbol.upper(),)).fetchone()
        return float(row[0]) if row else 0.0

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
