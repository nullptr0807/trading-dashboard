#!/usr/bin/env python3
"""Five-minute Moomoo reconciliation for the independent live-strategy ledger.

The script is safe to schedule before credentials exist: it never places orders.
Any reconciliation ambiguity freezes the control plane and exits non-zero.
"""
from __future__ import annotations

import json
import math
import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.live_logging import get_live_logger, log_event
from core.live_strategy_control import ControlRejected, LiveStrategyStore
from core.moomoo_audit import (
    finalize_preview, is_module_order, is_module_preview, module_preview_record,
)
from core.moomoo_client import MoomooClient

TERMINAL = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}
logger = get_live_logger("live.moomoo.sync", "moomoo-sync.jsonl")


def number(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        try:
            value = row.get(key)
            if value is not None:
                result = float(value)
                if math.isfinite(result):
                    return result
        except (TypeError, ValueError):
            pass
    return 0.0


def fee_total(row: dict[str, Any]) -> float:
    for key in ("total_fee", "fee_amount", "total_fees"):
        if row.get(key) is not None:
            return max(0.0, number(row, key))
    return max(0.0, sum(number(row, key) for key in (
        "commission", "platform_fee", "settlement_fee", "stamp_duty", "sec_fee", "taf_fee"
    )))


def reconcile(client: Any, store: LiveStrategyStore, ownership_proof=None) -> dict[str, Any]:
    snapshot = client.snapshot()
    if snapshot.get("activity_warnings"):
        raise ControlRejected("Moomoo history or fee data is incomplete")
    account_id = snapshot.get("account_id")

    def default_proof(order_id: str, preview_id: str) -> bool:
        if not account_id:
            return False
        record = module_preview_record(preview_id, account_id)
        if not record:
            return False
        payload = record["payload"]
        broker = next((item for item in snapshot.get("orders", [])
                       if str(item.get("order_id") or "") == order_id), {})
        if record.get("order_id") and str(record["order_id"]) != order_id:
            return False
        checks = [
            str(broker.get("code") or "").upper() == str(payload.get("code") or "").upper(),
            str(broker.get("trd_side") or "").upper() == str(payload.get("side") or "").upper(),
            abs(number(broker, "qty") - number(payload, "qty")) <= 1e-9,
            abs(number(broker, "price") - number(payload, "limit_price")) <= 1e-6,
            int(payload.get("account_id") or 0) == int(account_id),
        ]
        return all(checks)

    proof = ownership_proof or default_proof
    module_orders: dict[str, dict[str, Any]] = {}
    for row in snapshot.get("orders", []):
        remark = str(row.get("remark") or "")
        if not remark.startswith("dashboard:"):
            continue
        order_id = str(row.get("order_id") or "")
        preview_id = remark.rsplit(":", 1)[-1]
        if not order_id or not preview_id or not proof(order_id, preview_id):
            raise ControlRejected("Unproven dashboard order remark; possible ownership forgery")
        module_orders[order_id] = row
    fee_by_order = {str(row.get("order_id")): fee_total(row)
                    for row in snapshot.get("order_fees", []) if row.get("order_id") is not None}
    deals_by_order: dict[str, list[dict[str, Any]]] = {}
    for deal in snapshot.get("deals", []):
        oid = str(deal.get("order_id") or "")
        if oid in module_orders:
            deals_by_order.setdefault(oid, []).append(deal)
    if ownership_proof is None:
        for order_id, order in module_orders.items():
            preview_id = str(order.get("remark") or "").rsplit(":", 1)[-1]
            if is_module_preview(preview_id, account_id) and not is_module_order(order_id, account_id):
                status = str(order.get("order_status") or "").upper()
                failed = status in {"CANCELLED_ALL", "FAILED", "DISABLED", "DELETED"} and not deals_by_order.get(order_id)
                finalize_preview(preview_id, "failed" if failed else "accepted",
                                 None if failed else order_id)
    applied = 0
    for order_id, deals in deals_by_order.items():
        order = module_orders[order_id]
        total_qty = number(order, "dealt_qty") or sum(number(d, "deal_qty", "qty") for d in deals)
        if total_qty <= 0:
            raise ControlRejected("Module order has deals but no valid dealt quantity")
        total_fee = fee_by_order.get(order_id)
        if total_fee is None:
            raise ControlRejected("Moomoo fee record missing for a module order")
        for deal in deals:
            deal_ref = deal.get("deal_id")
            qty = number(deal, "deal_qty", "qty")
            price = number(deal, "deal_price", "price")
            side = str(deal.get("trd_side") or order.get("trd_side") or "").upper()
            symbol = str(deal.get("code") or order.get("code") or "").upper()
            if not deal_ref or side not in {"BUY", "SELL"} or not symbol or qty <= 0 or price <= 0:
                raise ControlRejected("Malformed module-tagged Moomoo deal")
            allocated_fee = total_fee * qty / total_qty
            if store.apply_fill(str(deal_ref), symbol, side, qty, price, allocated_fee):
                applied += 1
    owned = store.positions()
    broker_positions = {str(row.get("code") or "").upper(): number(row, "qty")
                        for row in snapshot.get("positions", [])}
    owned_symbols = {row["symbol"] for row in owned}
    external = [symbol for symbol, qty in broker_positions.items()
                if qty > 1e-9 and symbol not in owned_symbols]
    settings = getattr(client, "settings", None)
    shared_read_only = bool(
        settings
        and not settings.dedicated_account_confirmed
        and not settings.trading_enabled
        and not settings.auto_trading_enabled
    )
    if external and not shared_read_only:
        raise ControlRejected(
            "Dedicated strategy account contains external holdings; strong isolation proof failed"
        )
    if external:
        store.event(
            "shared_account_external_holdings", "moomoo_reconciler", "info",
            "Shared-account holdings observed in read-only mode and excluded from the strategy ledger",
            {"count": len(external)},
        )
    for row in owned:
        broker_qty = broker_positions.get(row["symbol"], 0.0)
        owned_qty = float(row["quantity"])
        if abs(broker_qty - owned_qty) > 1e-9:
            raise ControlRejected(
                "Broker quantity differs from strategy-owned quantity; possible external lot overlap or corporate action"
            )
    prices = {row["symbol"]: client.quote(row["symbol"])["last_price"] for row in owned}
    pending_buy = sum(
        max(0.0, number(row, "qty") - number(row, "dealt_qty")) * number(row, "price")
        for row in module_orders.values()
        if str(row.get("trd_side") or "").upper() == "BUY"
        and str(row.get("order_status") or "").upper() not in TERMINAL
    )
    store.set_reserved_buy_notional(pending_buy)
    state = store.mark_to_market(prices, sync_complete=True)
    cancellation = None
    settings = getattr(client, "settings", None)
    if (state.lifecycle == "FROZEN" and settings
            and settings.trade_api_token and settings.password_md5):
        try:
            cancellation = client.cancel_all_module_orders(settings.trade_api_token)
        except Exception as exc:
            cancellation = {"error": type(exc).__name__}
            store.event("risk_freeze_cancel_failed", "moomoo_reconciler", "critical",
                        "Risk freeze could not confirm cancellation of module orders", {})
    result = {"ok": True, "applied_fills": applied, "owned_positions": len(owned),
              "external_positions": len(external), "shared_read_only": shared_read_only,
              "equity": state.strategy_equity, "market_value": state.owned_market_value,
              "lifecycle": state.lifecycle, "freeze_reason": state.freeze_reason,
              "cancellation": cancellation}
    log_event(logger, "info", "moomoo_reconciliation_complete", **result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    store = LiveStrategyStore()
    client = MoomooClient(control_store=store)
    try:
        result = reconcile(client, store)
        if (args.verbose or
                (result["lifecycle"] == "FROZEN" and
                 result.get("freeze_reason") not in {"not_provisioned", "manual_freeze"})):
            print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        try:
            state = store.freeze("five_minute_reconciliation_failed", "moomoo_sync")
            store.event("sync_failed", "moomoo_sync", "critical",
                        "Moomoo five-minute reconciliation failed", {"error": str(exc)})
            lifecycle = state.lifecycle
        except Exception:
            lifecycle = "UNKNOWN"
        log_event(logger, "critical", "moomoo_reconciliation_failed",
                  error=str(exc), lifecycle=lifecycle)
        print(json.dumps({"ok": False, "alert": "LIVE_SYSTEM_FROZEN",
                          "reason": "five_minute_reconciliation_failed",
                          "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
