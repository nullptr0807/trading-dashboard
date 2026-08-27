"""Moomoo real-account API. Mutations are fail-closed and independently audited."""
from __future__ import annotations

import asyncio
import hmac
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.moomoo_audit import (
    append_audit, finalize_preview, known_module_order_ids,
    nav_history, recent_audit, record_nav_snapshot, unresolved_preview_count,
)
from core.moomoo_client import (
    BrokerOutcomeUnknown, LiveTradeRejected, MoomooClient, MoomooUnavailable,
)
from core.live_strategy_control import ControlRejected

router = APIRouter(prefix="/api/live-account", tags=["live_account"])
_client = MoomooClient()


def get_client() -> MoomooClient:
    return _client


def _require_read_token(provided: str) -> None:
    client = get_client()
    expected = client.settings.read_api_token
    if not expected or not provided:
        raise HTTPException(status_code=401, detail="Live-account read authorization required")
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Invalid live-account read authorization")


def _require_control_token(provided: str) -> None:
    expected = get_client().settings.control_api_token
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Invalid live-control authorization token")


class OrderPreviewRequest(BaseModel):
    code: str = Field(min_length=1, max_length=24)
    side: Literal["BUY", "SELL", "buy", "sell"]
    qty: int = Field(gt=0, le=1_000_000)
    limit_price: float = Field(gt=0, le=1_000_000)
    session: Literal["RTH", "OVERNIGHT", "rth", "overnight"] = "RTH"


class OrderPlaceRequest(BaseModel):
    preview_token: str = Field(min_length=20, max_length=8192)
    confirmation: str


class CancelOrderRequest(BaseModel):
    confirmation: str


class ControlActionRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=3, max_length=500)


class ConfigUpdateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    patch: dict[str, Any]
    reason: str = Field(min_length=3, max_length=500)


class EventRequest(BaseModel):
    event_type: str = Field(min_length=1, max_length=80)
    source: str = Field(min_length=1, max_length=80)
    severity: str = Field(pattern="^(debug|info|warning|critical)$")
    message: str = Field(min_length=1, max_length=1000)
    details: dict[str, Any] = Field(default_factory=dict)


def _error(exc: Exception):
    if isinstance(exc, LiveTradeRejected):
        raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(status_code=503, detail=str(exc))


@router.get("/status")
async def live_status(x_moomoo_read_token: str = Header(default="")):
    client = get_client()
    status = await asyncio.to_thread(client.status)
    granted = bool(client.settings.read_api_token and x_moomoo_read_token and
                   hmac.compare_digest(client.settings.read_api_token, x_moomoo_read_token))
    if not granted:
        status["account_id"] = None
    status["read_access_granted"] = granted
    return {"status": status, "policy": client.public_policy(),
            "data_source": "Moomoo OpenD only", "paper_ledger_used": False}


@router.get("/control")
async def live_control(x_moomoo_read_token: str = Header(default=""),
                       event_limit: int = Query(200, ge=1, le=1000)):
    _require_read_token(x_moomoo_read_token)
    store = get_client().control
    try:
        return {
            "state": asdict(await asyncio.to_thread(store.snapshot)),
            "config": await asyncio.to_thread(store.config),
            "owned_positions": await asyncio.to_thread(store.positions),
            "equity": await asyncio.to_thread(store.equity_history),
            "paper_series": await asyncio.to_thread(store.paper_series),
            "events": await asyncio.to_thread(store.recent_events, event_limit),
            "hard_limits": {"initial_capital": 10_000, "exposure_cap": 10_000,
                            "loss_floor": 7_500, "regular_hours_only": True},
        }
    except ControlRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/strategy")
async def live_strategy(event_limit: int = Query(100, ge=1, le=200),
                        fill_limit: int = Query(200, ge=1, le=1000)):
    """Public strategy sub-ledger only; never returns broker account data."""
    store = get_client().control
    try:
        events = await asyncio.to_thread(store.recent_events, event_limit)
        try:
            market_quote = await asyncio.to_thread(get_client().quote, "US.SPY")
            market_status = {
                "state": str(market_quote.get("market_state") or "UNKNOWN").upper(),
                "security_status": str(market_quote.get("sec_status") or "UNKNOWN").upper(),
                "updated_at": market_quote.get("update_time"),
            }
        except MoomooUnavailable:
            market_status = {"state": "UNKNOWN", "security_status": "UNKNOWN", "updated_at": None}
        public_events = [
            {key: row.get(key) for key in ("ts", "event_type", "source", "severity", "message")}
            for row in events
        ]
        return {
            "state": asdict(await asyncio.to_thread(store.snapshot)),
            "config": await asyncio.to_thread(store.config),
            "owned_positions": await asyncio.to_thread(store.positions),
            "execution_summary": await asyncio.to_thread(store.execution_summary),
            "performance_summary": await asyncio.to_thread(store.performance_summary),
            "market_status": market_status,
            "fills": await asyncio.to_thread(store.fills, fill_limit),
            "equity": await asyncio.to_thread(store.equity_history),
            "paper_series": await asyncio.to_thread(store.paper_series),
            "events": public_events,
            "hard_limits": {"initial_capital": 10_000, "exposure_cap": 10_000,
                            "loss_floor": 7_500, "regular_hours_only": True},
            "data_scope": "strategy_subledger_only",
        }
    except ControlRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.put("/control/config")
async def update_live_config(req: ConfigUpdateRequest,
                             x_moomoo_control_token: str = Header(default="")):
    _require_control_token(x_moomoo_control_token)
    try:
        return await asyncio.to_thread(
            get_client().control.update_config, req.patch, req.expected_version,
            "dashboard", req.reason,
        )
    except ControlRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/control/freeze")
async def freeze_live_system(req: ControlActionRequest,
                             x_moomoo_control_token: str = Header(default="")):
    _require_control_token(x_moomoo_control_token)
    if req.confirmation != "FREEZE LIVE TRADING":
        raise HTTPException(status_code=400, detail="Type FREEZE LIVE TRADING to confirm")
    state = await asyncio.to_thread(get_client().control.freeze, req.reason, "dashboard")
    cancellation = {"attempted": False, "reason": "broker_not_configured"}
    client = get_client()
    if client.settings.trade_api_token and client.settings.password_md5:
        try:
            cancellation = await asyncio.to_thread(
                client.cancel_all_module_orders, client.settings.trade_api_token,
            )
            cancellation["attempted"] = True
        except Exception as exc:
            cancellation = {"attempted": True, "error": type(exc).__name__}
            client.control.event("freeze_cancel_failed", "dashboard", "critical",
                                 "Freeze could not confirm cancellation of every module order", {})
    return {"state": asdict(state), "cancellation": cancellation}


@router.post("/control/unfreeze")
async def unfreeze_live_system(req: ControlActionRequest,
                               x_moomoo_control_token: str = Header(default="")):
    _require_control_token(x_moomoo_control_token)
    if req.confirmation != "UNFREEZE LIVE TRADING":
        raise HTTPException(status_code=400, detail="Type UNFREEZE LIVE TRADING to confirm")
    try:
        client = get_client()
        if client.settings.account_isolation_mode not in {"dedicated", "shared_restricted"}:
            raise ControlRejected("An accepted Moomoo account isolation mode is required before unfreezing")
        if await asyncio.to_thread(unresolved_preview_count):
            raise ControlRejected("Broker outcomes require reconciliation before unfreezing")
        await asyncio.to_thread(client.current_sync_fingerprint)
        state = await asyncio.to_thread(client.control.unfreeze, req.reason, "dashboard")
        return {"state": asdict(state)}
    except ControlRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/control/events")
async def append_live_event(req: EventRequest,
                            x_moomoo_control_token: str = Header(default="")):
    _require_control_token(x_moomoo_control_token)
    event_id = await asyncio.to_thread(
        get_client().control.event, req.event_type, req.source, req.severity,
        req.message, req.details,
    )
    return {"event_id": event_id}


@router.post("/control/cleanup")
async def cleanup_live_strategy(req: ControlActionRequest,
                                x_moomoo_control_token: str = Header(default="")):
    _require_control_token(x_moomoo_control_token)
    try:
        client = get_client()
        await asyncio.to_thread(client.control.freeze, "cleanup_requested", "dashboard")
        if await asyncio.to_thread(unresolved_preview_count):
            raise ControlRejected("Broker outcomes require reconciliation before cleanup")
        if client.settings.account_id <= 0:
            raise ControlRejected("A configured Moomoo account is required to prove zero active orders before cleanup")
        broker = await asyncio.to_thread(client.snapshot)
        if broker.get("activity_warnings"):
            raise ControlRejected("Complete Moomoo order history is required before cleanup")
        known = await asyncio.to_thread(known_module_order_ids, client.settings.account_id)
        terminal = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}
        active = [row for row in broker.get("orders", [])
                  if str(row.get("order_status") or "").upper() not in terminal
                  and (str(row.get("order_id") or "") in known
                       or str(row.get("remark") or "").startswith("dashboard:"))]
        broker_orders_clear = not active
        archive = await asyncio.to_thread(
            client.control.cleanup, req.confirmation, "dashboard", req.reason,
            broker_orders_clear,
        )
        return {"cleaned": True, "archive_name": archive.name,
                "state": asdict(get_client().control.snapshot())}
    except (ControlRejected, MoomooUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/snapshot")
async def live_snapshot(x_moomoo_read_token: str = Header(default="")):
    _require_read_token(x_moomoo_read_token)
    try:
        client = get_client()
        data = await asyncio.to_thread(client.snapshot_cached, 300)
        await asyncio.to_thread(
            record_nav_snapshot, data["account_id"], data.get("account") or {},
            client.settings.currency,
        )
        data["nav_history"] = await asyncio.to_thread(nav_history, data["account_id"])
        return data
    except (MoomooUnavailable, LiveTradeRejected) as exc:
        _error(exc)


@router.get("/quote/{code}")
async def live_quote(code: str, x_moomoo_read_token: str = Header(default="")):
    _require_read_token(x_moomoo_read_token)
    try:
        return await asyncio.to_thread(get_client().quote, code)
    except (MoomooUnavailable, LiveTradeRejected) as exc:
        _error(exc)


@router.get("/audit")
async def live_audit(limit: int = Query(50, ge=1, le=200),
                     x_moomoo_read_token: str = Header(default="")):
    _require_read_token(x_moomoo_read_token)
    return {"items": await asyncio.to_thread(recent_audit, limit)}


@router.post("/orders/preview")
async def preview_order(req: OrderPreviewRequest, x_moomoo_read_token: str = Header(default="")):
    _require_read_token(x_moomoo_read_token)
    payload = req.model_dump()
    try:
        result = await asyncio.to_thread(get_client().preview_order, **payload)
        append_audit("preview", True, {**payload, "account_id": result.get("account_id")})
        return result
    except (MoomooUnavailable, LiveTradeRejected) as exc:
        append_audit("preview", False, {**payload, "error": str(exc)})
        _error(exc)


@router.post("/orders/place")
async def place_order(req: OrderPlaceRequest, x_moomoo_trade_token: str = Header(default="")):
    if req.confirmation != "PLACE LIVE ORDER":
        append_audit("place_rejected", False, {"error": "confirmation phrase mismatch"})
        raise HTTPException(status_code=400, detail="Type PLACE LIVE ORDER to confirm")
    client = get_client()
    preview = {}
    try:
        preview = client.verify_preview(req.preview_token)
        client.authenticate_trade_token(x_moomoo_trade_token)
        # Durable attempt record is a prerequisite to any broker mutation.
        append_audit("place_attempt", False, preview)
        result = await asyncio.to_thread(client.place_order, req.preview_token, x_moomoo_trade_token)
        order = result.get("order") or {}
        try:
            finalize_preview(preview["preview_id"], "accepted", str(order.get("order_id") or ""))
            append_audit("place", True, {**preview, "order_id": order.get("order_id")})
            result["audit_status"] = "recorded"
        except Exception:
            result["audit_status"] = "pending_reconciliation"
        return result
    except BrokerOutcomeUnknown as exc:
        if preview.get("preview_id"):
            try:
                finalize_preview(preview["preview_id"], "unknown")
                append_audit("place_unknown", False, {**preview, "error": str(exc)})
            except Exception:
                pass
        _error(exc)
    except (MoomooUnavailable, LiveTradeRejected) as exc:
        if preview.get("preview_id"):
            try:
                finalize_preview(preview["preview_id"], "failed")
            except Exception:
                pass
        append_audit("place", False, {**preview, "error": str(exc)})
        _error(exc)


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, req: CancelOrderRequest,
                       x_moomoo_trade_token: str = Header(default="")):
    if req.confirmation != "CANCEL LIVE ORDER":
        append_audit("cancel_rejected", False, {"order_id": order_id, "error": "confirmation phrase mismatch"})
        raise HTTPException(status_code=400, detail="Type CANCEL LIVE ORDER to confirm")
    try:
        get_client().authenticate_trade_token(x_moomoo_trade_token)
        if not await asyncio.to_thread(get_client().module_order_authorized, order_id):
            raise LiveTradeRejected("Only orders placed by this module can be cancelled here")
        append_audit("cancel_attempt", False, {"order_id": order_id})
        result = await asyncio.to_thread(get_client().cancel_order, order_id, x_moomoo_trade_token)
        append_audit("cancel", True, {"order_id": order_id})
        return result
    except BrokerOutcomeUnknown as exc:
        try:
            append_audit("cancel_unknown", False, {"order_id": order_id, "error": str(exc)})
        except Exception:
            pass
        _error(exc)
    except (MoomooUnavailable, LiveTradeRejected) as exc:
        append_audit("cancel", False, {"order_id": order_id, "error": str(exc)})
        _error(exc)
