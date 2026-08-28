"""Fail-closed B16 automatic execution over the independent live sub-ledger."""
from __future__ import annotations

import fcntl
import hashlib
import math
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from core.live_auto_planner import LiveOrderPlan, OrderIntent, plan_live_orders
from core.live_order_service import dispatch_signed_preview
from core.live_signal_adapter import SignalBatch, load_b16_signal_batch
from core.live_strategy_control import AUTO_INTENT_TERMINAL, ControlRejected, LiveStrategyStore
from core.moomoo_client import (
    BrokerOutcomeUnknown,
    LiveTradeRejected,
    MoomooClient,
    MoomooUnavailable,
)

TERMINAL_ORDERS = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}
AUTO_LOCK_PATH = Path("/tmp/moomoo_b16_live_auto.lock")


class AutoExecutionError(RuntimeError):
    pass


@contextmanager
def auto_cycle_lock(path: str | Path = AUTO_LOCK_PATH):
    lock_path = Path(path)
    with lock_path.open("a+") as handle:
        try:
            lock_path.chmod(0o600)
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _number(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        try:
            value = row.get(key)
            if value is not None and math.isfinite(float(value)):
                return float(value)
        except (TypeError, ValueError):
            pass
    return 0.0


def _module_orders(snapshot: dict[str, Any], strategy_id: str) -> list[dict[str, Any]]:
    prefix = f"dashboard:{strategy_id}:"
    return [row for row in snapshot.get("orders", [])
            if str(row.get("remark") or "").startswith(prefix)]


def _pending_orders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if str(row.get("order_status") or "").upper() in TERMINAL_ORDERS:
            continue
        result.append({
            "symbol": str(row.get("code") or ""),
            "side": str(row.get("trd_side") or ""),
            "quantity": int(_number(row, "qty")),
            "filled_quantity": int(_number(row, "dealt_qty")),
            "limit_price": _number(row, "price"),
        })
    return result


def _daily_module_notional(rows: list[dict[str, Any]], now: datetime) -> float:
    today = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    total = 0.0
    for row in rows:
        created = str(row.get("create_time") or row.get("create_time_str") or "")
        if created[:10] == today:
            total += _number(row, "qty") * _number(row, "price")
    return total


def _cooldowns(store: LiveStrategyStore, hours: int, now: datetime) -> dict[str, str]:
    if hours <= 0:
        return {}
    result: dict[str, str] = {}
    for row in store.list_auto_order_intents(limit=1000):
        if row.get("purpose") != "STOP_LOSS" or row.get("status") != "FILLED":
            continue
        try:
            completed = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
            if completed.tzinfo is None:
                continue
            until = completed.astimezone(timezone.utc) + timedelta(hours=hours)
            if until > now:
                result[str(row["symbol"])] = until.isoformat()
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _opened_at(store: LiveStrategyStore) -> dict[str, str]:
    # Conservative approximation from proven fills. If live min_hold_days > 0 and
    # a timestamp cannot be proven, the planner refuses a rank-exit for that name.
    result: dict[str, str] = {}
    quantity: dict[str, float] = {}
    for row in reversed(store.fills(limit=1000)):
        symbol = str(row["symbol"])
        old = quantity.get(symbol, 0.0)
        delta = float(row["quantity"]) * (1 if row["side"] == "BUY" else -1)
        new = max(0.0, old + delta)
        if old <= 0 < new:
            result[symbol] = str(row["applied_at"])
        if new <= 0:
            result.pop(symbol, None)
        quantity[symbol] = new
    return result


def _order_for_intent(snapshot: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any] | None:
    expected_remark = f"dashboard:{intent['strategy_id']}:{intent['preview_id']}"
    order = next((row for row in snapshot.get("orders", [])
                  if str(row.get("remark") or "") == expected_remark), None)
    if order is None:
        return None
    proven = all([
        str(order.get("code") or "").upper() == str(intent["symbol"]).upper(),
        str(order.get("trd_side") or "").upper() == str(intent["side"]).upper(),
        abs(_number(order, "qty") - float(intent["order_qty"])) <= 1e-9,
        abs(_number(order, "price") - float(intent["limit_price"])) <= 1e-6,
    ])
    if not proven:
        raise ControlRejected("Broker order does not match the reserved automatic intent")
    return order


def recover_auto_intents(
    store: LiveStrategyStore,
    snapshot: dict[str, Any],
    *,
    reconciliation_complete: bool = False,
) -> dict[str, Any] | None:
    """Recover proven intents; only a complete reconciliation may make them terminal."""
    blocker = None
    for intent in reversed(store.list_auto_order_intents(limit=1000)):
        status = str(intent["status"])
        if status in AUTO_INTENT_TERMINAL:
            continue
        if status == "RESERVED":
            blocker = intent
            continue
        preview_id = str(intent.get("preview_id") or "")
        try:
            order = _order_for_intent(snapshot, intent) if preview_id else None
        except ControlRejected:
            if status != "UNKNOWN":
                store.mark_auto_intent_unknown(intent["intent_id"], "BROKER_ORDER_PROOF_MISMATCH")
            store.freeze("auto_intent_broker_proof_mismatch", "auto_executor")
            return store.get_auto_order_intent(intent["intent_id"])
        if not order:
            if status in {"DISPATCHING", "UNKNOWN"}:
                if status != "UNKNOWN":
                    store.mark_auto_intent_unknown(intent["intent_id"], "BROKER_ORDER_NOT_PROVEN")
                store.freeze("auto_intent_broker_outcome_unknown", "auto_executor")
                return store.get_auto_order_intent(intent["intent_id"])
            blocker = intent
            continue
        order_status = str(order.get("order_status") or "").upper()
        dealt = _number(order, "dealt_qty")
        ordered = _number(order, "qty")
        if order_status in TERMINAL_ORDERS and not reconciliation_complete:
            blocker = intent
            continue
        if order_status == "FILLED_ALL" or (ordered > 0 and dealt >= ordered - 1e-9):
            if status != "FILLED":
                store.mark_auto_intent_filled(intent["intent_id"])
            continue
        if order_status in {"CANCELLED_ALL", "CANCELLED_PART"}:
            store.mark_auto_intent_cancelled(
                intent["intent_id"], "PARTIAL_CANCEL" if dealt > 0 else "BROKER_CANCELLED",
            )
            continue
        if order_status in {"FAILED", "DISABLED", "DELETED"}:
            store.mark_auto_intent_failed(intent["intent_id"], "BROKER_REJECTED")
            continue
        if dealt > 0 and status != "PARTIAL":
            store.mark_auto_intent_partial(intent["intent_id"])
        elif dealt <= 0 and status in {"DISPATCHING", "UNKNOWN"}:
            store.mark_auto_intent_acked(intent["intent_id"])
        current = store.get_auto_order_intent(intent["intent_id"])
        if current and current["status"] in {"ACKED", "PARTIAL"}:
            store.handoff_auto_intent_reservation(intent["intent_id"])
        blocker = store.get_auto_order_intent(intent["intent_id"])
    return blocker


class LiveAutoExecutor:
    def __init__(
        self,
        client: MoomooClient,
        store: LiveStrategyStore,
        *,
        signal_loader: Callable[..., SignalBatch] = load_b16_signal_batch,
        reconcile_fn: Callable[[MoomooClient, LiveStrategyStore], dict[str, Any]] | None = None,
    ):
        self.client = client
        self.store = store
        self.signal_loader = signal_loader
        self.reconcile_fn = reconcile_fn

    def _build_plan(self, now: datetime) -> tuple[SignalBatch, LiveOrderPlan, dict[str, Any]]:
        batch = self.signal_loader(strategy_id="B16", as_of=now)
        config = self.store.config()
        state = asdict(self.store.snapshot())
        owned_rows = self.store.positions()
        owned = {str(row["symbol"]): row for row in owned_rows}
        snapshot = self.client.snapshot()
        module_orders = _module_orders(snapshot, "B16")
        cfg = config["values"]
        quote_count = max(int(cfg["top_n"]), int(cfg["top_n"]) * int(cfg["hold_band_mult"]))
        quote_symbols = set(owned)
        quote_symbols.update(batch.buy_candidates[:quote_count])
        quotes = self.client.quotes(tuple(sorted(quote_symbols)))
        plan = plan_live_orders(
            batch, config, state, owned, quotes, _pending_orders(module_orders),
            opened_at=_opened_at(self.store),
            cooldown_until=_cooldowns(self.store, int(cfg["stop_cooldown_hours"]), now),
            now=now,
        )
        return batch, plan, {"snapshot": snapshot, "module_orders": module_orders, "quotes": quotes}

    def shadow(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        batch, plan, context = self._build_plan(current)
        return {
            "mode": "shadow", "strategy_id": plan.strategy_id,
            "signal_source_date": batch.source_date,
            "signal_batch_id": batch.batch_id,
            "factor_set_hash": batch.factor_set_hash,
            "plan_id": plan.plan_id,
            "intents": [asdict(intent) for intent in plan.intents],
            "module_pending_orders": len(_pending_orders(context["module_orders"])),
            "broker_mutation": False,
        }

    def execute_one(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not self.client.settings.trading_enabled or not self.client.settings.auto_trading_enabled:
            raise AutoExecutionError("Live auto execution requires both trading and auto switches")
        if not self.client.settings.trade_api_token:
            raise AutoExecutionError("Live auto execution token is not configured")
        if self.reconcile_fn is None:
            raise AutoExecutionError("Live auto execution requires pre/post reconciliation")
        eastern = current.astimezone(ZoneInfo("America/New_York"))
        minute = eastern.hour * 60 + eastern.minute
        if eastern.weekday() >= 5 or not (570 <= minute < 960):
            raise AutoExecutionError("Live auto execution is restricted to US RTH")
        market_probe = self.client.quote("US.SPY")
        if str(market_probe.get("market_state") or "").upper() not in {"MORNING", "AFTERNOON"}:
            raise AutoExecutionError("Moomoo does not report US regular-hours trading")
        self.reconcile_fn(self.client, self.store)
        snapshot = self.client.snapshot()
        blocker = recover_auto_intents(self.store, snapshot)
        if blocker and blocker["status"] != "RESERVED":
            return {"mode": "execute", "status": "blocked_by_unresolved_intent",
                    "intent_id_hash": hashlib.sha256(str(blocker["intent_id"]).encode()).hexdigest()}
        batch, plan, context = self._build_plan(current)
        if blocker and (int(blocker["config_version"]) != int(plan.config_version)
                        or blocker["signal_batch_id"] != batch.batch_id
                        or blocker["factor_set_hash"] != batch.factor_set_hash):
            self.store.mark_auto_intent_cancelled(blocker["intent_id"], "STALE_SIGNAL_BATCH")
            blocker = None
        chosen: OrderIntent | None = None
        intent_row = blocker
        if intent_row is None:
            if not plan.intents:
                return {"mode": "execute", "status": "no_action", "plan_id": plan.plan_id}
            chosen = plan.intents[0]
            symbol = self.client.normalize_code(chosen.symbol)
            owned = self.store.owned_quantity(symbol)
            target_qty = owned - chosen.quantity if chosen.side == "SELL" else owned + chosen.quantity
            module_orders = context["module_orders"]
            pending_buy = sum(
                max(0.0, _number(row, "qty") - _number(row, "dealt_qty")) * _number(row, "price")
                for row in module_orders
                if str(row.get("trd_side") or "").upper() == "BUY"
                and str(row.get("order_status") or "").upper() not in TERMINAL_ORDERS
            )
            pending_sell = sum(
                max(0.0, _number(row, "qty") - _number(row, "dealt_qty"))
                for row in module_orders
                if str(row.get("code") or "").upper() == symbol
                and str(row.get("trd_side") or "").upper() == "SELL"
                and str(row.get("order_status") or "").upper() not in TERMINAL_ORDERS
            )
            intent_row = self.store.create_auto_order_intent(
                strategy_id="B16", config_version=plan.config_version,
                signal_batch_id=batch.batch_id, signal_source_date=batch.source_date,
                factor_set_hash=batch.factor_set_hash, symbol=symbol, side=chosen.side,
                purpose=chosen.purpose, target_qty=max(0, target_qty),
                order_qty=chosen.quantity, limit_price=chosen.limit_price,
                broker_pending_buy_notional=pending_buy,
                broker_pending_sell_qty=pending_sell,
                daily_order_notional=_daily_module_notional(module_orders, current),
            )
        else:
            chosen = OrderIntent(
                intent_row["symbol"], intent_row["side"], int(intent_row["order_qty"]),
                float(intent_row["limit_price"]), intent_row["purpose"],
            )
        if intent_row["status"] != "RESERVED":
            return {"mode": "execute", "status": "intent_not_dispatchable"}
        broker_accepted = False
        try:
            preview = self.client.preview_order(
                code=chosen.symbol, side=chosen.side, qty=chosen.quantity,
                limit_price=chosen.limit_price, session="RTH",
                auto_intent_id=intent_row["intent_id"],
            )
            self.store.mark_auto_intent_dispatching(intent_row["intent_id"], preview["preview_id"])
            result = dispatch_signed_preview(
                self.client, preview["preview_token"], self.client.settings.trade_api_token,
                source="auto_executor",
            )
            broker_accepted = True
            self.store.mark_auto_intent_acked(intent_row["intent_id"])
            self.reconcile_fn(self.client, self.store)
            order = result.get("order") or {}
            final = self.store.get_auto_order_intent(intent_row["intent_id"])
            order_id = str(order.get("order_id") or "")
            return {
                "mode": "execute", "status": "broker_accepted",
                "symbol": chosen.symbol, "side": chosen.side, "quantity": chosen.quantity,
                "limit_price": chosen.limit_price,
                "intent_id_hash": hashlib.sha256(str(intent_row["intent_id"]).encode()).hexdigest(),
                "order_ref_hash": hashlib.sha256(order_id.encode()).hexdigest() if order_id else None,
                "intent_status": final["status"] if final else "FILLED",
            }
        except BrokerOutcomeUnknown:
            self.store.mark_auto_intent_unknown(intent_row["intent_id"], "BROKER_OUTCOME_UNKNOWN")
            self.store.freeze("auto_broker_outcome_unknown", "auto_executor")
            raise
        except MoomooUnavailable:
            current_intent = self.store.get_auto_order_intent(intent_row["intent_id"])
            if broker_accepted:
                # ACKED remains the global order blocker until the scheduled
                # reconciler proves the final Broker result. A transient API or
                # quote miss after acceptance must not freeze the whole strategy.
                self.store.event(
                    "auto_post_broker_reconciliation_deferred", "auto_executor", "warning",
                    "Broker accepted an order; final reconciliation deferred after transient API failure",
                    {"symbol": chosen.symbol, "side": chosen.side},
                )
            elif current_intent and current_intent["status"] in {"RESERVED", "DISPATCHING"}:
                self.store.mark_auto_intent_failed(intent_row["intent_id"], "PRE_BROKER_REJECTED")
            raise
        except (LiveTradeRejected, ControlRejected):
            current_intent = self.store.get_auto_order_intent(intent_row["intent_id"])
            if broker_accepted:
                if current_intent and current_intent["status"] == "DISPATCHING":
                    self.store.mark_auto_intent_unknown(
                        intent_row["intent_id"], "POST_BROKER_LOCAL_FAILURE",
                    )
                self.store.freeze(
                    "auto_post_broker_reconciliation_failed", "auto_executor",
                    preserve_existing=True,
                )
            elif current_intent and current_intent["status"] in {"RESERVED", "DISPATCHING"}:
                self.store.mark_auto_intent_failed(intent_row["intent_id"], "PRE_BROKER_REJECTED")
            raise
        except Exception:
            current_intent = self.store.get_auto_order_intent(intent_row["intent_id"])
            if current_intent and current_intent["status"] == "DISPATCHING":
                self.store.mark_auto_intent_unknown(
                    intent_row["intent_id"], "UNCLASSIFIED_DISPATCH_FAILURE",
                )
            self.store.freeze("auto_unclassified_dispatch_failure", "auto_executor")
            raise
