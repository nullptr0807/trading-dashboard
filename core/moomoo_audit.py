"""Dedicated append-only audit log for Moomoo live-account actions."""
from __future__ import annotations

import json
import os
import sqlite3
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUDIT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "moomoo_live_audit.db"


def _connect(path: str | Path = AUDIT_DB_PATH) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS live_order_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            success INTEGER NOT NULL,
            account_id TEXT,
            code TEXT,
            side TEXT,
            qty REAL,
            limit_price REAL,
            order_id TEXT,
            detail TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS live_nav_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            account_id TEXT NOT NULL,
            currency TEXT NOT NULL,
            total_assets REAL NOT NULL,
            cash REAL,
            market_value REAL,
            unrealized_pl REAL,
            UNIQUE(account_id, ts)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS live_order_previews (
            preview_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            expires_at REAL NOT NULL,
            status TEXT NOT NULL,
            account_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT,
            order_id TEXT,
            outcome TEXT
        )
    """)
    return con


def append_audit(action: str, success: bool, detail: dict[str, Any],
                 *, path: str | Path = AUDIT_DB_PATH) -> int:
    safe = dict(detail or {})
    for secret in ("preview_token", "auth_token", "password", "password_md5"):
        safe.pop(secret, None)
    with _connect(path) as con:
        cur = con.execute(
            "INSERT INTO live_order_audit(ts,action,success,account_id,code,side,qty,limit_price,order_id,detail) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), action, int(success),
             str(safe.get("account_id") or "") or None, safe.get("code"), safe.get("side"),
             safe.get("qty"), safe.get("limit_price"), str(safe.get("order_id") or "") or None,
             json.dumps(safe, ensure_ascii=False, default=str, sort_keys=True)),
        )
        return int(cur.lastrowid or 0)


def recent_audit(limit: int = 100, *, path: str | Path = AUDIT_DB_PATH) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with _connect(path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id,ts,action,success,account_id,code,side,qty,limit_price,order_id,detail "
            "FROM live_order_audit ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def record_nav_snapshot(account_id: int | str, account: dict[str, Any], currency: str,
                        *, path: str | Path = AUDIT_DB_PATH) -> None:
    def number(*keys: str) -> float:
        for key in keys:
            try:
                value = account.get(key)
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                pass
        return 0.0

    total = number("total_assets", "total_asset", "net_asset")
    if total <= 0:
        return
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
    with _connect(path) as con:
        con.execute(
            "INSERT OR REPLACE INTO live_nav_snapshots"
            "(ts,account_id,currency,total_assets,cash,market_value,unrealized_pl) "
            "VALUES(?,?,?,?,?,?,?)",
            (now, str(account_id), currency, total,
             number("cash", "cash_balance"), number("market_val", "market_value"),
             number("unrealized_pl", "unrealized_pl_val")),
        )


def nav_history(account_id: int | str, limit: int = 2000,
                *, path: str | Path = AUDIT_DB_PATH) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 10_000))
    with _connect(path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT ts,currency,total_assets,cash,market_value,unrealized_pl "
            "FROM live_nav_snapshots WHERE account_id=? ORDER BY ts DESC LIMIT ?",
            (str(account_id), limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def register_preview(payload: dict[str, Any], ttl_seconds: int,
                     *, path: str | Path = AUDIT_DB_PATH) -> None:
    preview_id = str(payload.get("preview_id") or "")
    if not preview_id:
        raise ValueError("preview_id is required")
    now = datetime.now(timezone.utc)
    with _connect(path) as con:
        con.execute(
            "INSERT INTO live_order_previews(preview_id,created_at,expires_at,status,account_id,payload) "
            "VALUES(?,?,?,?,?,?)",
            (preview_id, now.isoformat(), now.timestamp() + int(ttl_seconds), "ready",
             str(payload.get("account_id") or ""),
             json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)),
        )


def claim_preview(preview_id: str, *, path: str | Path = AUDIT_DB_PATH) -> bool:
    now = datetime.now(timezone.utc)
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            "UPDATE live_order_previews SET status='claimed',claimed_at=? "
            "WHERE preview_id=? AND status='ready' AND expires_at>=?",
            (now.isoformat(), str(preview_id), now.timestamp()),
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def finalize_preview(preview_id: str, outcome: str, order_id: str | None = None,
                     *, path: str | Path = AUDIT_DB_PATH) -> None:
    with _connect(path) as con:
        con.execute(
            "UPDATE live_order_previews SET status=?,completed_at=?,order_id=?,outcome=? "
            "WHERE preview_id=? AND status IN ('claimed','reconcile')",
            ("accepted" if outcome == "accepted" else "reconcile" if outcome == "unknown" else "failed",
             datetime.now(timezone.utc).isoformat(), order_id, outcome, str(preview_id)),
        )


def is_module_order(order_id: str, account_id: int | str,
                    *, path: str | Path = AUDIT_DB_PATH) -> bool:
    with _connect(path) as con:
        row = con.execute(
            "SELECT 1 FROM live_order_previews "
            "WHERE order_id=? AND account_id=? AND status='accepted' LIMIT 1",
            (str(order_id), str(account_id)),
        ).fetchone()
    return row is not None


def is_module_preview(preview_id: str, account_id: int | str,
                      *, path: str | Path = AUDIT_DB_PATH) -> bool:
    with _connect(path) as con:
        row = con.execute(
            "SELECT 1 FROM live_order_previews WHERE preview_id=? AND account_id=? "
            "AND status IN ('claimed','accepted','reconcile') LIMIT 1",
            (str(preview_id), str(account_id)),
        ).fetchone()
    return row is not None


def module_preview_record(preview_id: str, account_id: int | str,
                          *, path: str | Path = AUDIT_DB_PATH) -> dict[str, Any] | None:
    with _connect(path) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT preview_id,account_id,status,order_id,payload FROM live_order_previews "
            "WHERE preview_id=? AND account_id=? AND status IN ('claimed','accepted','reconcile') LIMIT 1",
            (str(preview_id), str(account_id)),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    return result


def unresolved_preview_count(*, path: str | Path = AUDIT_DB_PATH) -> int:
    with _connect(path) as con:
        row = con.execute(
            "SELECT COUNT(*) FROM live_order_previews WHERE status='reconcile'"
        ).fetchone()
    return int(row[0])


def known_module_order_ids(account_id: int | str,
                           *, path: str | Path = AUDIT_DB_PATH) -> set[str]:
    with _connect(path) as con:
        rows = con.execute(
            "SELECT order_id FROM live_order_previews WHERE account_id=? "
            "AND order_id IS NOT NULL AND status IN ('accepted','reconcile')",
            (str(account_id),),
        ).fetchall()
    return {str(row[0]) for row in rows}


@contextmanager
def account_execution_lock(account_id: int | str, *, path: str | Path = AUDIT_DB_PATH):
    """Cross-process lock for the account risk-check + broker mutation region."""
    lock_path = Path(str(path) + f".{account_id}.execution.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
