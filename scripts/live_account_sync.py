#!/usr/bin/env python3
"""Five-minute Moomoo reconciliation for the independent live-strategy ledger.

The script is safe to schedule before credentials exist: it never places orders.
Any reconciliation ambiguity freezes the control plane and exits non-zero.
"""
from __future__ import annotations

import json
import math
import re
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


def fee_number(row: dict[str, Any], key: str, *, source: str) -> float:
    """Parse an explicitly present fee field without coercing bad data to zero."""
    try:
        result = float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlRejected(f"Moomoo {source} contains an invalid numeric value") from exc
    if not math.isfinite(result):
        raise ControlRejected(f"Moomoo {source} contains an invalid numeric value")
    return result


def fee_total(row: dict[str, Any]) -> float:
    aliases = ("total_fee", "fee_amount", "total_fees", "commission")
    values = [
        fee_number(row, key, source="fee record")
        for key in aliases if row.get(key) is not None
    ]
    if any(value < 0 for value in values):
        raise ControlRejected("Moomoo fee record contains a negative amount")
    if values:
        if any(value != values[0] for value in values[1:]):
            raise ControlRejected("Moomoo fee record contains conflicting fee aliases")
        return values[0]
    values = [
        fee_number(row, key, source="fee record") if row.get(key) is not None else 0.0
        for key in ("platform_fee", "settlement_fee", "stamp_duty", "sec_fee", "taf_fee")
    ]
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


def deal_fee(deal: dict[str, Any]) -> float | None:
    """Return a Broker-provided per-deal fee, never an inferred allocation."""
    aliases = ("deal_fee", "fee_amount", "total_fee", "total_fees", "commission")
    values = [
        fee_number(deal, key, source="deal fee")
        for key in aliases if deal.get(key) is not None
    ]
    if any(value < 0 for value in values):
        raise ControlRejected("Moomoo deal fee contains a negative amount")
    if values:
        if any(value != values[0] for value in values[1:]):
            raise ControlRejected("Moomoo deal fee contains conflicting fee aliases")
        return values[0]
    component_keys = ("platform_fee", "settlement_fee", "stamp_duty", "sec_fee", "taf_fee")
    if any(deal.get(key) is not None for key in component_keys):
        values = [
            fee_number(deal, key, source="deal fee") if deal.get(key) is not None else 0.0
            for key in component_keys
        ]
        if any(value < 0 for value in values):
            raise ControlRejected("Moomoo deal fee contains a negative component")
        return sum(values)
    return None


def normalized_deal_identity(deal: dict[str, Any]) -> tuple[str, str, float, float, float | None]:
    """Canonical economic identity; Broker references are deliberately excluded."""
    return (
        str(deal.get("code") or "").strip().upper(),
        str(deal.get("trd_side") or "").strip().upper(),
        deal_number(deal, "deal_qty", "qty"),
        deal_number(deal, "deal_price", "price"),
        deal_fee(deal),
    )


def order_number(order: dict[str, Any], key: str) -> float:
    """Read required module-order economics without coercing bad data to zero."""
    try:
        value = float(order[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlRejected("Moomoo module order contains an invalid numeric value") from exc
    if not math.isfinite(value):
        raise ControlRejected("Moomoo module order contains an invalid numeric value")
    return value


def preview_number(payload: dict[str, Any], key: str) -> float:
    """Read immutable local preview economics without permissive coercion."""
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlRejected("Local module preview contains an invalid numeric value") from exc
    if not math.isfinite(value):
        raise ControlRejected("Local module preview contains an invalid numeric value")
    return value


def position_number(position: dict[str, Any]) -> float:
    """Parse all populated position quantity aliases and require agreement."""
    values = []
    for key in ("qty", "position_qty"):
        if position.get(key) is None:
            continue
        try:
            value = float(position[key])
        except (TypeError, ValueError) as exc:
            raise ControlRejected("Broker position contains an invalid quantity") from exc
        if not math.isfinite(value) or value < 0:
            raise ControlRejected("Broker position contains an invalid quantity")
        values.append(value)
    if not values:
        raise ControlRejected("Broker position omitted its quantity")
    if any(value != values[0] for value in values[1:]):
        raise ControlRejected("Broker position contains conflicting quantity aliases")
    return values[0]


def position_symbol(position: dict[str, Any]) -> str:
    """Return a canonical, independently scopeable Broker symbol."""
    symbol = str(position.get("code") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}\.[A-Z0-9][A-Z0-9.-]{0,31}", symbol):
        raise ControlRejected("Broker position omitted or contains an invalid symbol")
    return symbol


def _failure_code(exc: Exception) -> str:
    """Return a bounded public error class; never serialize the exception text."""
    if isinstance(exc, ControlRejected):
        return "RECONCILIATION_REJECTED"
    if isinstance(exc, TimeoutError):
        return "RECONCILIATION_TIMEOUT"
    if isinstance(exc, ConnectionError):
        return "BROKER_UNAVAILABLE"
    return "RECONCILIATION_INTERNAL_ERROR"


def reconcile(client: Any, store: LiveStrategyStore, ownership_proof=None) -> dict[str, Any]:
    snapshot = client.snapshot()
    snapshot_observed_at = utcnow()
    if snapshot.get("activity_warnings"):
        raise ControlRejected("Moomoo history or fee data is incomplete")
    account_id = snapshot.get("account_id")

    proven_records: dict[str, dict[str, Any]] = {}

    def default_proof(order_id: str, preview_id: str) -> bool:
        if not account_id:
            return False
        record = module_preview_record(preview_id, account_id)
        if not record:
            return False
        if (str(record.get("preview_id") or "") != preview_id
                or str(record.get("account_id") or "") != str(account_id)
                or str(record.get("status") or "") not in {"claimed", "accepted", "reconcile"}):
            return False
        bound_order_id = str(record.get("order_id") or "")
        if bound_order_id and bound_order_id != order_id:
            return False
        # Durable claimed/reconcile state proves local ownership independently
        # of untrusted Broker economics. Parse and compare those only after the
        # order is classified as owned so malformed values latch permanently.
        proven_records[order_id] = record
        return True

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
        previous_order = module_orders.get(order_id)
        symbol = str(row.get("code") or "").strip().upper()
        side = str(row.get("trd_side") or "").strip().upper()
        invalid_identity = []
        if not symbol:
            invalid_identity.append("symbol")
        if side not in {"BUY", "SELL"}:
            invalid_identity.append("side")
        if invalid_identity:
            store.latch_reconciliation_snapshot_conflict(
                "order_economics", symbol, invalid_identity,
                "Authorized module order contains invalid identity fields",
            )
            raise ControlRejected("Moomoo module order must provide a valid symbol and side")
        try:
            quantity = order_number(row, "qty")
            price = order_number(row, "price")
            dealt_quantity = order_number(row, "dealt_qty")
            if quantity <= 0 or price < 0 or dealt_quantity < 0:
                raise ControlRejected("Moomoo module order contains negative or invalid economics")
            if dealt_quantity > quantity + 1e-9:
                raise ControlRejected("Moomoo module order contains contradictory quantities")
        except ControlRejected:
            store.latch_reconciliation_snapshot_conflict(
                "numeric", symbol, ["quantity", "price"],
                "Authorized module order contains invalid numeric economics",
            )
            raise
        if ownership_proof is None:
            payload = proven_records[order_id].get("payload") or {}
            try:
                preview_qty = preview_number(payload, "qty")
                preview_price = preview_number(payload, "limit_price")
            except ControlRejected:
                store.latch_reconciliation_snapshot_conflict(
                    "numeric", symbol, ["quantity", "price"],
                    "Authorized module preview contains invalid immutable economics",
                )
                raise
            conflicting_preview_fields = []
            if symbol != str(payload.get("code") or "").strip().upper():
                conflicting_preview_fields.append("symbol")
            if side != str(payload.get("side") or "").strip().upper():
                conflicting_preview_fields.append("side")
            if str(payload.get("account_id") or "") != str(account_id):
                conflicting_preview_fields.append("order_ownership")
            if abs(quantity - preview_qty) > 1e-9:
                conflicting_preview_fields.append("quantity")
            if abs(price - preview_price) > 1e-6:
                conflicting_preview_fields.append("price")
            if conflicting_preview_fields:
                store.latch_reconciliation_snapshot_conflict(
                    "order_economics", symbol, conflicting_preview_fields,
                    "Authorized module order differs from its immutable local preview",
                )
                raise ControlRejected("Moomoo module order differs from its authorized preview")
        if previous_order is not None:
            fields = ("symbol", "side", "quantity", "price", "quantity")
            old_identity = (
                str(previous_order.get("code") or "").strip().upper(),
                str(previous_order.get("trd_side") or "").strip().upper(),
                order_number(previous_order, "qty"), order_number(previous_order, "price"),
                order_number(previous_order, "dealt_qty"),
            )
            new_identity = (
                symbol, str(row.get("trd_side") or "").strip().upper(),
                quantity, price, dealt_quantity,
            )
            if old_identity != new_identity:
                store.latch_reconciliation_snapshot_conflict(
                    "order_economics", str(row.get("code") or ""),
                    list(dict.fromkeys(
                        name for name, old, new in zip(fields, old_identity, new_identity)
                        if old != new
                    )),
                    "Broker snapshot repeated an order reference with conflicting economics",
                )
                raise ControlRejected(
                    "Moomoo returned a duplicate order reference with conflicting economics"
                )
            continue
        module_orders[order_id] = row
    # Contradictory Broker economics are permanent reconciliation conflicts,
    # unlike an unavailable API.  Persist the conflict before propagating it.
    for deal in snapshot.get("deals", []):
        order_id = str(deal.get("order_id") or "")
        order = module_orders.get(order_id)
        if order is None:
            continue
        symbol = str(deal.get("code") or "").strip().upper()
        deal_side = str(deal.get("trd_side") or "").strip().upper()
        invalid_identity = []
        if not str(deal.get("deal_id") or "").strip():
            invalid_identity.append("deal_identity")
        if not symbol:
            invalid_identity.append("symbol")
        if deal_side not in {"BUY", "SELL"}:
            invalid_identity.append("side")
        if invalid_identity:
            store.latch_reconciliation_snapshot_conflict(
                "order_economics", symbol or str(order.get("code") or ""),
                invalid_identity,
                "Authorized module deal contains invalid identity fields",
            )
            raise ControlRejected(
                "Moomoo deal must explicitly provide symbol and side; identity is required"
            )
        try:
            if deal_number(deal, "deal_qty", "qty") <= 0:
                raise ControlRejected("Moomoo deal contains a negative or invalid quantity")
        except ControlRejected:
            store.latch_reconciliation_snapshot_conflict(
                "numeric", symbol, ["quantity"],
                "Broker deal quantity aliases are invalid or contradictory",
            )
            raise
        try:
            if deal_number(deal, "deal_price", "price") <= 0:
                raise ControlRejected("Moomoo deal contains a negative or invalid price")
        except ControlRejected:
            store.latch_reconciliation_snapshot_conflict(
                "numeric", symbol, ["price"],
                "Broker deal price aliases are invalid or contradictory",
            )
            raise
        try:
            deal_fee(deal)
        except ControlRejected:
            store.latch_reconciliation_snapshot_conflict(
                "numeric", symbol, ["fee"],
                "Broker deal fee aliases are invalid, negative, or contradictory",
            )
            raise
        order_symbol = str(order.get("code") or "").strip().upper()
        order_side = str(order.get("trd_side") or "").strip().upper()
        conflicting = []
        if symbol != order_symbol:
            conflicting.append("symbol")
        if deal_side != order_side:
            conflicting.append("side")
        if conflicting:
            store.latch_reconciliation_snapshot_conflict(
                "order_economics", symbol or order_symbol, conflicting,
                "Broker deal economics differ from the authorized module order",
            )
            if conflicting == ["symbol"]:
                raise ControlRejected("Moomoo deal symbol differs from its authorized order")
            if conflicting == ["side"]:
                raise ControlRejected("Moomoo deal side differs from its authorized order")
            raise ControlRejected(
                "Moomoo deal symbol and side differ from its authorized order"
            )
    fee_by_order: dict[str, float] = {}
    for row in snapshot.get("order_fees", []):
        if row.get("order_id") is None:
            continue
        order_id = str(row.get("order_id"))
        if order_id not in module_orders:
            continue
        try:
            total = fee_total(row)
        except ControlRejected:
            order = module_orders[order_id]
            store.latch_reconciliation_snapshot_conflict(
                "numeric", str(order.get("code") or ""), ["fee"],
                "Authorized module order fee aliases are invalid, negative, or contradictory",
            )
            raise
        if order_id in fee_by_order and fee_by_order[order_id] != total:
            order = module_orders[order_id]
            store.latch_reconciliation_snapshot_conflict(
                "numeric", str(order.get("code") or ""), ["fee"],
                "Broker snapshot repeated an order fee reference with conflicting totals",
            )
            raise ControlRejected(
                "Moomoo returned a duplicate fee reference with conflicting totals"
            )
        fee_by_order[order_id] = total
    deals_by_order: dict[str, list[dict[str, Any]]] = {}
    deals_by_reference: dict[str, dict[str, Any]] = {}
    for deal in snapshot.get("deals", []):
        oid = str(deal.get("order_id") or "")
        if oid in module_orders:
            deal_ref = str(deal.get("deal_id") or "")
            if deal_ref and deal_ref in deals_by_reference:
                previous = deals_by_reference[deal_ref]
                if str(previous.get("order_id") or "") != oid:
                    store.latch_reconciliation_snapshot_conflict(
                        "order_economics", str(deal.get("code") or ""),
                        ["order_ownership"],
                        "Broker deal reference appeared under multiple authorized orders",
                    )
                    raise ControlRejected(
                        "Moomoo deal reference appeared under multiple orders"
                    )
                previous_identity = normalized_deal_identity(previous)
                replay_identity = normalized_deal_identity(deal)
                if previous_identity != replay_identity:
                    field_names = ("symbol", "side", "quantity", "price", "fee")
                    store.latch_snapshot_deal_conflict(
                        str(deal.get("code") or previous.get("code") or ""),
                        [name for name, old, new in zip(
                            field_names, previous_identity, replay_identity,
                        ) if old != new],
                    )
                continue
            if deal_ref:
                deals_by_reference[deal_ref] = deal
            deals_by_order.setdefault(oid, []).append(deal)

    # Broker order rows can lead deal-detail rows. Never publish a sync proof
    # until both views agree, including when no deal row has arrived at all.
    for order_id, order in module_orders.items():
        order_qty = order_number(order, "qty")
        dealt_qty = order_number(order, "dealt_qty")
        deal_qty_total = sum(
            deal_number(deal, "deal_qty", "qty")
            for deal in deals_by_order.get(order_id, [])
        )
        if order_qty <= 0 or dealt_qty < 0 or dealt_qty > order_qty + 1e-9:
            store.latch_reconciliation_snapshot_conflict(
                "numeric", str(order.get("code") or ""), ["quantity"],
                "Authorized module order quantities are invalid or contradictory",
            )
            raise ControlRejected("Module order has an invalid authorized or dealt quantity")
        order_status = str(order.get("order_status") or "").upper()
        if order_status == "FILLED_ALL" and abs(dealt_qty - order_qty) > 1e-9:
            store.latch_reconciliation_snapshot_conflict(
                "numeric", str(order.get("code") or ""), ["quantity"],
                "Module order status contradicts its filled quantity",
            )
            raise ControlRejected("Module order filled status leads complete fill details")
        if abs(dealt_qty - deal_qty_total) > 1e-9:
            store.latch_reconciliation_snapshot_conflict(
                "numeric", str(order.get("code") or ""), ["quantity"],
                "Module order filled quantity contradicts its deal details",
            )
            raise ControlRejected("Module order dealt quantity differs from deal detail total")
        direct_deal_fees = [deal_fee(deal) for deal in deals_by_order.get(order_id, [])]
        all_deal_fees_known = bool(direct_deal_fees) and all(
            value is not None for value in direct_deal_fees
        )
        if (dealt_qty > 0 and order_id not in fee_by_order and not all_deal_fees_known
                and order_status in TERMINAL):
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
    order_fee_observations = []
    for order_id, deals in deals_by_order.items():
        order = module_orders[order_id]
        deal_qty_total = sum(deal_number(d, "deal_qty", "qty") for d in deals)
        dealt_qty = order_number(order, "dealt_qty")
        order_qty = order_number(order, "qty")
        if dealt_qty <= 0 or deal_qty_total <= 0:
            raise ControlRejected("Module order has deals but no valid dealt quantity")
        if abs(dealt_qty - deal_qty_total) > 1e-9:
            raise ControlRejected("Module order dealt quantity differs from deal detail total")
        if order_qty <= 0 or dealt_qty > order_qty + 1e-9:
            raise ControlRejected("Module order dealt quantity exceeds authorized order quantity")
        direct_fees = [deal_fee(deal) for deal in deals]
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
            stable_fee = deal_fee(deal)
            staged_fills.append({
                "external_reference": str(deal_ref), "symbol": symbol,
                "side": side, "quantity": qty, "price": price,
                "fee": stable_fee if stable_fee is not None else 0.0,
                "fee_is_stable": stable_fee is not None,
                "external_order_reference": order_id,
            })
        if order_id in fee_by_order or all(value is not None for value in direct_fees):
            cumulative_fee = (fee_by_order[order_id] if order_id in fee_by_order
                              else sum(float(value) for value in direct_fees if value is not None))
            order_fee_observations.append({
                "external_order_reference": order_id,
                "symbol": str(order.get("code") or "").strip().upper(),
                "side": str(order.get("trd_side") or "").strip().upper(),
                "cumulative_fee": cumulative_fee,
                "finalized": str(order.get("order_status") or "").upper() in TERMINAL,
            })
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
    prospective_symbols = owned_before | staged_symbols
    broker_positions: dict[str, float] = {}
    broker_position_sides: dict[str, str] = {}
    external_symbols: set[str] = set()
    for row in snapshot.get("positions", []):
        symbol = ""
        try:
            symbol = position_symbol(row)
            if shared_external_allowed and symbol not in prospective_symbols:
                # Scope is proven from identity alone. Never parse unrelated
                # holdings' side or quantity into the strategy trust domain.
                external_symbols.add(symbol)
                continue
            quantity = position_number(row)
            side_values = [
                str(row[key]).strip().upper()
                for key in ("position_side", "side")
                if row.get(key) is not None
            ]
            if any(side not in {"LONG", "BUY"} for side in side_values):
                raise ControlRejected("Broker position contains an invalid side")
            canonical_side = "LONG"
            if symbol in broker_positions:
                raise ControlRejected("Broker position duplicate conflicts with scoped holdings")
            broker_positions[symbol] = quantity
            broker_position_sides[symbol] = canonical_side
        except ControlRejected as exc:
            fields = []
            text = str(exc).lower()
            if "symbol" in text:
                fields.append("symbol")
            if "side" in text:
                fields.append("side")
            if "quantity" in text or "economics" in text or "duplicate" in text:
                fields.append("quantity")
            store.latch_reconciliation_snapshot_conflict(
                "positions", symbol, fields or ["quantity"],
                "Broker position snapshot contains invalid or conflicting identity/economics",
            )
            raise
    unrelated_external = [
        symbol for symbol, qty in broker_positions.items()
        if qty > 1e-9 and symbol not in owned_before and symbol not in staged_symbols
    ]
    if unrelated_external and not shared_external_allowed:
        raise ControlRejected(
            "Dedicated strategy account contains external holdings; strong isolation proof failed"
        )
    prices = {symbol: client.quote(symbol)["last_price"] for symbol in prospective_symbols}
    pending_buy = sum(
        max(0.0, order_number(row, "qty") - order_number(row, "dealt_qty"))
        * order_number(row, "price")
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
        order_fee_observations=order_fee_observations,
    )
    for preview_id, status, order_id in preview_finalizations:
        finalize_preview(preview_id, status, order_id)
    auto_recovered = _recover_intents_and_transient_freeze(
        store, snapshot, account_isolation_mode=account_isolation_mode,
    )
    owned = store.positions()
    owned_symbols = {row["symbol"] for row in owned}
    external = sorted(external_symbols | {
        symbol for symbol, qty in broker_positions.items()
        if qty > 1e-9 and symbol not in owned_symbols
    })
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
        error_code = _failure_code(exc)
        try:
            state = store.freeze(
                "five_minute_reconciliation_failed", "moomoo_sync", preserve_existing=True,
            )
            store.event(
                "sync_failed", "moomoo_sync", "critical",
                "Moomoo five-minute reconciliation failed",
                {"error_code": error_code},
            )
            lifecycle = state.lifecycle
        except Exception:
            lifecycle = "UNKNOWN"
        log_event(
            logger, "critical", "moomoo_reconciliation_failed",
            error_code=error_code, lifecycle=lifecycle,
        )
        print(json.dumps({"ok": False, "alert": "LIVE_SYSTEM_FROZEN",
                          "reason": "five_minute_reconciliation_failed",
                          "error_code": error_code}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
