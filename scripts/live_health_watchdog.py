#!/usr/bin/env python3
"""Deterministic live-system health watchdog and sanitized AI snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.live_logging import get_live_logger, log_event, redact
from core.live_strategy_control import LiveStrategyStore
from core.moomoo_audit import AUDIT_DB_PATH
from core.moomoo_client import MoomooClient

STATE_FILE = ROOT / "data" / "live_health_alert_state.json"
TERMINAL = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}
logger = get_live_logger("live.health.watchdog", "health-watchdog.jsonl")


def parse_time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except ValueError:
        return None


def unknown_mutations() -> int:
    if not AUDIT_DB_PATH.exists():
        return 0
    con = sqlite3.connect(f"file:{AUDIT_DB_PATH}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT COUNT(*) FROM live_order_previews WHERE status='reconcile'").fetchone()
        return int(row[0])
    finally:
        con.close()


def diagnose(mutate: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    store = LiveStrategyStore()
    state = store.snapshot()
    settings = MoomooClient().settings
    problems = []
    if state.strategy_equity <= state.loss_floor:
        problems.append("LOSS_FLOOR_BREACH")
    if state.owned_market_value + state.reserved_buy_notional > state.exposure_cap + 1e-6:
        problems.append("EXPOSURE_CAP_BREACH")
    if unknown_mutations():
        problems.append("BROKER_OUTCOME_REQUIRES_RECONCILIATION")
    if state.lifecycle == "ACTIVE":
        synced = parse_time(state.last_sync_at)
        if not synced or (now - synced).total_seconds() > 7 * 60:
            problems.append("FIVE_MINUTE_SYNC_STALE")
        try:
            client = MoomooClient(control_store=store)
            snapshot = client.snapshot()
            if snapshot.get("activity_warnings"):
                problems.append("MOOMOO_HISTORY_INCOMPLETE")
            for order in snapshot.get("orders", []):
                if not str(order.get("remark") or "").startswith("dashboard:"):
                    continue
                if str(order.get("order_status") or "").upper() in TERMINAL:
                    continue
                created = parse_time(order.get("create_time") or order.get("create_time_str"))
                if not created or (now - created).total_seconds() > 15 * 60:
                    problems.append("MODULE_ORDER_STUCK_OVER_15_MIN")
                    break
        except Exception:
            problems.append("MOOMOO_UNAVAILABLE_WHILE_ACTIVE")
    elif state.lifecycle == "FROZEN" and state.freeze_reason not in {"not_provisioned", "manual_freeze"}:
        problems.append("SYSTEM_FROZEN:" + str(state.freeze_reason))
    result = {
        "checked_at": now.isoformat(), "healthy": not problems,
        "problems": sorted(set(problems)),
        "state": {"lifecycle": state.lifecycle, "freeze_reason": state.freeze_reason,
                  "strategy_equity": state.strategy_equity,
                  "owned_market_value": state.owned_market_value,
                  "reserved_buy_notional": state.reserved_buy_notional,
                  "last_sync_at": state.last_sync_at, "config_version": state.config_version},
        "opend_configured": bool(settings.account_id),
    }
    if problems and mutate:
        store.freeze("health_watchdog:" + ",".join(sorted(set(problems))), "health_watchdog")
        cancellation = {"attempted": False}
        if settings.trade_api_token and settings.password_md5:
            try:
                cancellation = MoomooClient(control_store=store).cancel_all_module_orders(settings.trade_api_token)
                cancellation["attempted"] = True
            except Exception as exc:
                cancellation = {"attempted": True, "error": type(exc).__name__}
                store.event("watchdog_cancel_failed", "health_watchdog", "critical",
                            "Watchdog could not confirm cancellation of module orders", {})
        result["cancellation"] = cancellation
        log_event(logger, "critical", "health_watchdog_freeze", problems=problems,
                  cancellation=cancellation)
    elif mutate:
        log_event(logger, "info", "health_watchdog_ok", lifecycle=state.lifecycle)
    return redact(result)


def should_alert(result: dict) -> bool:
    if result["healthy"]:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        return False
    digest = hashlib.sha256(json.dumps(result["problems"], sort_keys=True).encode()).hexdigest()
    previous = {}
    if STATE_FILE.exists():
        try:
            previous = json.loads(STATE_FILE.read_text())
        except Exception:
            previous = {}
    now = datetime.now(timezone.utc).timestamp()
    alert = previous.get("digest") != digest or now - float(previous.get("last_alert", 0)) >= 3600
    STATE_FILE.write_text(json.dumps({"digest": digest, "last_alert": now}))
    os.chmod(STATE_FILE, 0o600)
    return alert


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", action="store_true", help="always emit sanitized diagnostic JSON")
    args = parser.parse_args()
    result = diagnose(mutate=True)
    if args.snapshot or should_alert(result):
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # Alerts are emitted in stdout; keep exit zero to avoid scheduler-level
    # duplicate error notifications on every tick while an incident persists.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
