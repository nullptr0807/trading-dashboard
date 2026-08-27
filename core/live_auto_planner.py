"""Pure deterministic order planner for the independent B16 live ledger."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from core.live_signal_adapter import SignalBatch


class LivePlanError(RuntimeError):
    """Raised when live-only planner inputs are unsafe or inconsistent."""


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: int
    limit_price: float
    purpose: str
    order_type: str = "LIMIT"
    time_in_force: str = "DAY"
    session: str = "RTH"


@dataclass(frozen=True)
class LiveOrderPlan:
    strategy_id: str
    config_version: int
    signal_batch_id: str
    plan_id: str
    intents: tuple[OrderIntent, ...]


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value):
        return asdict(value)
    try:
        return vars(value)
    except TypeError as exc:
        raise LivePlanError(f"Invalid {name}") from exc


def _finite(value: Any, name: str, *, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise LivePlanError(f"Invalid {name}") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise LivePlanError(f"Invalid {name}")
    return result


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    number = _finite(value, name)
    if isinstance(value, bool) or number != int(number) or number < minimum:
        raise LivePlanError(f"Invalid {name}")
    return int(number)


def _canonical(symbol: Any) -> str:
    result = str(symbol).strip().upper()
    if result.startswith("US."):
        result = result[3:]
    if not result:
        raise LivePlanError("Invalid symbol")
    return result


def _timestamp(value: Any, name: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LivePlanError(f"Invalid {name}") from exc
    if result.tzinfo is None:
        raise LivePlanError(f"Invalid {name}")
    return result.astimezone(timezone.utc)


def _quote(quotes: Mapping[str, Any], canonical_symbol: str) -> tuple[float, float, float]:
    found: Any = None
    for key, value in quotes.items():
        if _canonical(key) == canonical_symbol:
            found = value
            break
    if found is None:
        raise LivePlanError(f"Missing quote for {canonical_symbol}")
    if isinstance(found, Mapping):
        last = _finite(found.get("last_price"), "quote price", minimum=0.0000001)
        bid = _finite(found.get("bid_price", last), "bid price", minimum=0.0000001)
        ask = _finite(found.get("ask_price", last), "ask price", minimum=0.0000001)
        return last, bid, ask
    price = _finite(found, "quote price", minimum=0.0000001)
    return price, price, price


def _pending_quantities(orders: Sequence[Mapping[str, Any]]) -> tuple[dict[str, int], dict[str, int], float]:
    buys: dict[str, int] = {}
    sells: dict[str, int] = {}
    reserved_buy_notional = 0.0
    for raw in orders:
        order = _mapping(raw, "pending strategy order")
        symbol = _canonical(order.get("symbol"))
        side = str(order.get("side", "")).upper()
        if side not in {"BUY", "SELL"}:
            raise LivePlanError("Invalid pending strategy order side")
        quantity = _integer(order.get("quantity"), "pending quantity", 1)
        filled = _integer(order.get("filled_quantity", order.get("dealt_quantity", 0)),
                          "pending filled quantity")
        remaining = quantity - filled
        if remaining < 0:
            raise LivePlanError("Pending filled quantity exceeds quantity")
        if not remaining:
            continue
        target = buys if side == "BUY" else sells
        target[symbol] = target.get(symbol, 0) + remaining
        if side == "BUY":
            price = _finite(order.get("limit_price"), "pending buy limit price", minimum=0.0000001)
            reserved_buy_notional += remaining * price
    return buys, sells, reserved_buy_notional


def plan_live_orders(
    signal_batch: SignalBatch,
    live_config: Mapping[str, Any],
    live_state: Mapping[str, Any],
    owned_positions: Mapping[str, Mapping[str, Any]],
    quotes: Mapping[str, Any],
    pending_strategy_orders: Sequence[Mapping[str, Any]],
    opened_at: Mapping[str, Any],
    cooldown_until: Mapping[str, Any],
    now: datetime,
) -> LiveOrderPlan:
    """Create live-only order intents without mutating inputs or calling a broker."""
    current_time = now
    if current_time.tzinfo is None:
        raise LivePlanError("now must be timezone-aware")
    current_time = current_time.astimezone(timezone.utc)
    cfg_envelope = _mapping(live_config, "live config")
    cfg = _mapping(cfg_envelope.get("values", cfg_envelope), "live config values")
    state = _mapping(live_state, "live state")
    if "version" not in cfg_envelope or "config_version" not in state:
        raise LivePlanError("Live config and state versions are required")
    config_version = _integer(cfg_envelope["version"], "config version")
    if config_version != _integer(state["config_version"], "state config version"):
        raise LivePlanError("Live config and state versions do not match")
    strategy_id = str(cfg.get("strategy_id", ""))
    if strategy_id != "B16" or signal_batch.strategy_id != "B16" or state.get("strategy_id") != "B16":
        raise LivePlanError("Live strategy and signal batch must both be B16")
    if state.get("lifecycle") != "ACTIVE":
        raise LivePlanError("Live strategy must be ACTIVE")

    top_n = _integer(cfg.get("top_n"), "top_n", 1)
    hold_mult = _integer(cfg.get("hold_band_mult"), "hold_band_mult", 1)
    min_hold_days = _integer(cfg.get("min_hold_days"), "min_hold_days")
    position_pct = _finite(cfg.get("position_target_pct"), "position_target_pct", minimum=0)
    gross_pct = _finite(cfg.get("gross_target_pct"), "gross_target_pct", minimum=0)
    stop_pct = _finite(cfg.get("stop_loss_pct"), "stop_loss_pct", minimum=0)
    _integer(cfg.get("stop_cooldown_hours"), "stop_cooldown_hours")
    if position_pct > 1 or gross_pct > 1 or stop_pct >= 1:
        raise LivePlanError("Invalid live allocation or stop configuration")
    if top_n * position_pct > gross_pct + 1e-12:
        raise LivePlanError("top_n × position_target_pct exceeds gross_target_pct")

    equity = _finite(state.get("strategy_equity"), "strategy equity", minimum=0)
    allocated_cash = _finite(state.get("allocated_cash"), "allocated cash", minimum=0)
    current_gross = _finite(state.get("owned_market_value"), "owned market value", minimum=0)
    state_reserved_buy = _finite(
        state.get("reserved_buy_notional"), "reserved buy notional", minimum=0,
    )
    pending_buys, pending_sells, reserved_buy_notional = _pending_quantities(pending_strategy_orders)
    reserved_buy_notional = max(reserved_buy_notional, state_reserved_buy)
    available_cash = max(0.0, allocated_cash - reserved_buy_notional)
    gross_remaining = max(0.0, equity * gross_pct - current_gross - reserved_buy_notional)

    positions: dict[str, tuple[str, int, float]] = {}
    for raw_symbol, raw_position in owned_positions.items():
        position = _mapping(raw_position, "owned position")
        canonical = _canonical(raw_symbol)
        quantity = _integer(position.get("quantity"), "owned quantity")
        average_cost = _finite(position.get("average_cost"), "average cost", minimum=0)
        if canonical in positions:
            raise LivePlanError("Duplicate owned position symbol")
        if quantity:
            positions[canonical] = (str(raw_symbol), quantity, average_cost)

    ranking = [_canonical(row.symbol) for row in signal_batch.ranking]
    if len(ranking) != len(set(ranking)):
        raise LivePlanError("Signal ranking contains duplicate symbols")
    rank_index = {symbol: index for index, symbol in enumerate(ranking)}
    hold_band = set(ranking[:top_n * hold_mult])
    opened = {_canonical(key): value for key, value in opened_at.items()}
    cooldown = {_canonical(key): value for key, value in cooldown_until.items()}

    sell_rows: list[tuple[int, int, OrderIntent]] = []
    retained: set[str] = set()
    exiting: set[str] = set()
    for canonical, (output_symbol, quantity, average_cost) in positions.items():
        last, bid, _ = _quote(quotes, canonical)
        stopped = average_cost > 0 and last <= average_cost * (1 - stop_pct)
        outside_band = canonical not in hold_band
        old_enough = False
        if canonical in opened:
            age = current_time - _timestamp(opened[canonical], "opened_at")
            old_enough = age.total_seconds() >= min_hold_days * 86400
        elif min_hold_days == 0:
            old_enough = True
        should_sell = stopped or (outside_band and old_enough)
        if should_sell:
            exiting.add(canonical)
            available = max(0, quantity - pending_sells.get(canonical, 0))
            if available:
                purpose = "STOP_LOSS" if stopped else "RANK_EXIT"
                priority = 0 if stopped else 1
                sell_rows.append((priority, rank_index.get(canonical, len(ranking)),
                                  OrderIntent(output_symbol, "SELL", available, bid, purpose)))
        else:
            retained.add(canonical)
    sell_rows.sort(key=lambda row: (row[0], row[1], _canonical(row[2].symbol)))
    intents: list[OrderIntent] = [row[2] for row in sell_rows]

    selected: list[str] = [symbol for symbol in ranking if symbol in retained][:top_n]
    for symbol in (_canonical(item) for item in signal_batch.buy_candidates):
        if len(selected) >= top_n:
            break
        if symbol in selected or symbol in exiting:
            continue
        if symbol in cooldown and current_time <= _timestamp(cooldown[symbol], "cooldown_until"):
            continue
        selected.append(symbol)

    target_notional = equity * position_pct
    for canonical in selected:
        if available_cash <= 0 or gross_remaining <= 0:
            break
        last, _, ask = _quote(quotes, canonical)
        del last
        held = positions.get(canonical, (canonical, 0, 0.0))[1]
        pending = pending_buys.get(canonical, 0)
        target_quantity = math.floor(target_notional / ask)
        desired = max(0, target_quantity - held - pending)
        affordable = math.floor(min(available_cash, gross_remaining) / ask)
        quantity = min(desired, affordable)
        if quantity <= 0:
            continue
        output_symbol = positions.get(canonical, (canonical, 0, 0.0))[0]
        intents.append(OrderIntent(output_symbol, "BUY", quantity, ask, "TARGET_BUY"))
        notional = quantity * ask
        available_cash -= notional
        gross_remaining -= notional

    payload = {
        "strategy_id": strategy_id,
        "config_version": config_version,
        "signal_batch_id": signal_batch.batch_id,
        "intents": [asdict(intent) for intent in intents],
    }
    plan_id = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"))
                             .encode("utf-8")).hexdigest()
    return LiveOrderPlan(strategy_id, config_version, signal_batch.batch_id, plan_id,
                         tuple(intents))
