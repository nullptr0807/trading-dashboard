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
from core.live_strategy_control import ControlRejected, LiveStrategyStore, utcnow
from core.moomoo_audit import (
    finalize_preview, is_module_order, is_module_preview, module_preview_record,
    unresolved_preview_count,
)
from core.moomoo_client import MoomooClient

TERMINAL = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}
logger = get_live_logger("live.moomoo.sync", "moomoo-sync.jsonl")

_RECOVERABLE_AUTO_FREEZE = "auto_post_broker_reconciliation_failed"
_WATCHDOG_FREEZE_PREFIX = "health_watchdog:SYSTEM_FROZEN:"


def _base_freeze_reason(reason: str | None) -> str:
    value = str(reason or "")
    while value.startswith(_WATCHDOG_FREEZE_PREFIX):
        value = value[len(_WATCHDOG_FREEZE_PREFIX):]
    return value


def _recover_intents_and_transient_freeze(
    store: LiveStrategyStore,
    snapshot: dict[str, Any],
    *,
    account_isolation_mode: str,
) -> bool:
    """Recover proven intents; release only the narrow transient auto freeze."""
    from core.live_auto_executor import recover_auto_intents

    blocker = recover_auto_intents(store, snapshot, reconciliation_complete=True)
    state = store.snapshot()
    if state.lifecycle != "FROZEN":
        return False
    if _base_freeze_reason(state.freeze_reason) != _RECOVERABLE_AUTO_FREEZE:
        return False
    if blocker is not None or store.auto_intent_reservations()["reserved_buy_notional"] > 1e-9:
        return False
    if account_isolation_mode not in {"dedicated", "shared_restricted"}:
        return False
    if unresolved_preview_count():
        return False
    active_module_orders = [
        row for row in snapshot.get("orders", [])
        if str(row.get("remark") or "").startswith("dashboard:")
        and str(row.get("order_status") or "").upper() not in TERMINAL
    ]
    if active_module_orders:
        return False
    store.unfreeze(
        "Automatic recovery after Broker order and strategy ledger fully reconciled",
        "moomoo_reconciler",
    )
    return True


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
            value = number(row, key)
            if value < 0:
                raise ControlRejected("Moomoo fee record contains a negative amount")
            return value
    values = [number(row, key) for key in (
        "commission", "platform_fee", "settlement_fee", "stamp_duty", "sec_fee", "taf_fee"
    )]
    if any(value < 0 for value in values):
        raise ControlRejected("Moomoo fee record contains a negative component")
    return sum(values)


def deal_number(deal: dict[str, Any], primary: str, alias: str) -> float:
    """Read deal aliases exactly, rejecting disagreement or non-finite values."""
    values = []
    for key in (primary, alias):
        if deal.get(key) is None:
            continue
        try:
            value = float(deal[key])
        except (TypeError, ValueError) as exc:
            raise ControlRejected("Moomoo deal contains an invalid numeric value") from exc
        if not math.isfinite(value):
            raise ControlRejected("Moomoo deal contains an invalid numeric value")
        values.append(value)
    if len(values) == 2 and values[0] != values[1]:
        raise ControlRejected("Moomoo deal contains conflicting numeric aliases")
    return values[0] if values else 0.0


def normalized_deal_identity(deal: dict[str, Any]) -> tuple[str, str, str, float, float]:
    """Canonical exact identity; no tolerance is allowed for fill economics."""
    return (
        str(deal.get("order_id") or ""),
        str(deal.get("code") or "").strip().upper(),
        str(deal.get("trd_side") or "").strip().upper(),
        deal_number(deal, "deal_qty", "qty"),
        deal_number(deal, "deal_price", "price"),
    )


def reconcile(client: Any, store: LiveStrategyStore, ownership_proof=None) -> dict[str, Any]:
    snapshot = client.snapshot()
    snapshot_observed_at = utcnow()
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
    deals_by_reference: dict[str, dict[str, Any]] = {}
    for deal in snapshot.get("deals", []):
        oid = str(deal.get("order_id") or "")
        if oid in module_orders:
            deal_ref = str(deal.get("deal_id") or "")
            if deal_ref and deal_ref in deals_by_reference:
                previous = deals_by_reference[deal_ref]
                if normalized_deal_identity(previous) != normalized_deal_identity(deal):
                    raise ControlRejected("Moomoo returned a conflicting duplicate deal reference")
                continue
            if deal_ref:
                deals_by_reference[deal_ref] = deal
            deals_by_order.setdefault(oid, []).append(deal)

    # Broker order rows can lead deal-detail rows. Never publish a sync proof
    # until both views agree, including when no deal row has arrived at all.
    for order_id, order in module_orders.items():
        order_qty = number(order, "qty")
        dealt_qty = number(order, "dealt_qty")
        deal_qty_total = sum(
            deal_number(deal, "deal_qty", "qty")
            for deal in deals_by_order.get(order_id, [])
        )
        if order_qty <= 0 or dealt_qty < 0 or dealt_qty > order_qty + 1e-9:
            raise ControlRejected("Module order has an invalid authorized or dealt quantity")
        order_status = str(order.get("order_status") or "").upper()
        if order_status == "FILLED_ALL" and abs(dealt_qty - order_qty) > 1e-9:
            raise ControlRejected("Module order filled status leads complete fill details")
        if abs(dealt_qty - deal_qty_total) > 1e-9:
            raise ControlRejected("Module order dealt quantity differs from deal detail total")
        if dealt_qty > 0 and order_id not in fee_by_order:
            raise ControlRejected("Moomoo fee record missing for a module order")

    preview_finalizations = []
    if ownership_proof is None:
        for order_id, order in module_orders.items():
            preview_id = str(order.get("remark") or "").rsplit(":", 1)[-1]
            if is_module_preview(preview_id, account_id) and not is_module_order(order_id, account_id):
                status = str(order.get("order_status") or "").upper()
                failed = status in {"CANCELLED_ALL", "FAILED", "DISABLED", "DELETED"} and not deals_by_order.get(order_id)
                preview_finalizations.append(
                    (preview_id, "failed" if failed else "accepted", None if failed else order_id)
                )
    staged_fills = []
    for order_id, deals in deals_by_order.items():
        order = module_orders[order_id]
        deal_qty_total = sum(deal_number(d, "deal_qty", "qty") for d in deals)
        dealt_qty = number(order, "dealt_qty")
        order_qty = number(order, "qty")
        if dealt_qty <= 0 or deal_qty_total <= 0:
            raise ControlRejected("Module order has deals but no valid dealt quantity")
        if abs(dealt_qty - deal_qty_total) > 1e-9:
            raise ControlRejected("Module order dealt quantity differs from deal detail total")
        if order_qty <= 0 or dealt_qty > order_qty + 1e-9:
            raise ControlRejected("Module order dealt quantity exceeds authorized order quantity")
        total_qty = deal_qty_total
        total_fee = fee_by_order.get(order_id)
        if total_fee is None:
            raise ControlRejected("Moomoo fee record missing for a module order")
        for deal in deals:
            deal_ref = deal.get("deal_id")
            qty = deal_number(deal, "deal_qty", "qty")
            price = deal_number(deal, "deal_price", "price")
            side = str(deal.get("trd_side") or "").strip().upper()
            symbol = str(deal.get("code") or "").strip().upper()
            if not symbol or side not in {"BUY", "SELL"}:
                raise ControlRejected("Moomoo deal must explicitly provide symbol and side")
            if symbol != str(order.get("code") or "").strip().upper():
                raise ControlRejected("Moomoo deal symbol differs from its authorized order")
            if side != str(order.get("trd_side") or "").strip().upper():
                raise ControlRejected("Moomoo deal side differs from its authorized order")
            if not deal_ref or side not in {"BUY", "SELL"} or not symbol or qty <= 0 or price <= 0:
                raise ControlRejected("Malformed module-tagged Moomoo deal")
            allocated_fee = total_fee * qty / total_qty
            staged_fills.append({
                "external_reference": str(deal_ref), "symbol": symbol,
                "side": side, "quantity": qty, "price": price, "fee": allocated_fee,
            })
    broker_positions = {str(row.get("code") or "").upper(): number(row, "qty")
                        for row in snapshot.get("positions", [])}
    settings = getattr(client, "settings", None)
    if not settings or not hasattr(settings, "account_mode"):
        raise ControlRejected("Explicit Moomoo account_mode is required for reconciliation")
    dedicated = bool(settings.dedicated_account_confirmed)
    shared_accepted = bool(settings and getattr(settings, "shared_account_risk_accepted", False))
    configured_mode = str(settings.account_mode).upper()
    account_isolation_mode = (
        "dedicated" if configured_mode == "DEDICATED" and dedicated
        and not shared_accepted else
        "shared_restricted" if configured_mode == "SHARED_RESTRICTED"
        and shared_accepted and not dedicated else
        "unverified" if configured_mode == "UNVERIFIED"
        and not dedicated and not shared_accepted else "invalid"
    )
    shared_read_only = bool(
        settings and account_isolation_mode == "unverified"
        and not settings.trading_enabled and not settings.auto_trading_enabled
    )
    if account_isolation_mode == "invalid":
        raise ControlRejected("Invalid account isolation configuration cannot produce a broker sync proof")
    shared_external_allowed = shared_read_only or account_isolation_mode == "shared_restricted"
    owned_before = {row["symbol"] for row in store.positions()}
    staged_symbols = {str(fill["symbol"]).upper() for fill in staged_fills}
    unrelated_external = [
        symbol for symbol, qty in broker_positions.items()
        if qty > 1e-9 and symbol not in owned_before and symbol not in staged_symbols
    ]
    if unrelated_external and not shared_external_allowed:
        raise ControlRejected(
            "Dedicated strategy account contains external holdings; strong isolation proof failed"
        )
    prospective_symbols = owned_before | staged_symbols
    prices = {symbol: client.quote(symbol)["last_price"] for symbol in prospective_symbols}
    pending_buy = sum(
        max(0.0, number(row, "qty") - number(row, "dealt_qty")) * number(row, "price")
        for row in module_orders.values()
        if str(row.get("trd_side") or "").upper() == "BUY"
        and str(row.get("order_status") or "").upper() not in TERMINAL
    )
    fingerprint_fn = getattr(client, "current_sync_fingerprint", None)
    fingerprint = str(fingerprint_fn()) if callable(fingerprint_fn) else "test-sync-fingerprint"
    store.observe_runtime_fingerprint(fingerprint)
    applied = store.apply_fill_batch(
        staged_fills, broker_positions, prices, pending_buy, fingerprint,
        allow_external_overlap=account_isolation_mode == "shared_restricted",
        account_isolation_mode=account_isolation_mode,
        quantity_observed_at=snapshot_observed_at,
    )
    for preview_id, status, order_id in preview_finalizations:
        finalize_preview(preview_id, status, order_id)
    auto_recovered = _recover_intents_and_transient_freeze(
        store, snapshot, account_isolation_mode=account_isolation_mode,
    )
    owned = store.positions()
    owned_symbols = {row["symbol"] for row in owned}
    external = [symbol for symbol, qty in broker_positions.items()
                if qty > 1e-9 and symbol not in owned_symbols]
    if external:
        store.event(
            "shared_account_external_holdings", "moomoo_reconciler", "info",
            "Shared-account holdings observed in read-only mode and excluded from the strategy ledger",
            {"count": len(external)},
        )
    state = store.snapshot()
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
              "account_isolation_mode": account_isolation_mode,
              "auto_recovered": auto_recovered,
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
            state = store.freeze(
                "five_minute_reconciliation_failed", "moomoo_sync", preserve_existing=True,
            )
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
