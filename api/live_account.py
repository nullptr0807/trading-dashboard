"""Moomoo real-account API. Mutations are fail-closed and independently audited."""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.moomoo_audit import (
    append_audit, finalize_preview, is_module_order,
    nav_history, recent_audit, record_nav_snapshot,
)
from core.moomoo_client import (
    BrokerOutcomeUnknown, LiveTradeRejected, MoomooClient, MoomooUnavailable,
)

router = APIRouter(prefix="/api/live-account", tags=["live_account"])
_client = MoomooClient()


def get_client() -> MoomooClient:
    return _client


def _require_read_token(provided: str) -> None:
    client = get_client()
    expected = client.settings.read_api_token
    if not expected or not provided:
        raise HTTPException(status_code=401, detail="Live-account read authorization required")
    import hmac
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Invalid live-account read authorization")


class OrderPreviewRequest(BaseModel):
    code: str = Field(min_length=1, max_length=24)
    side: Literal["BUY", "SELL", "buy", "sell"]
    qty: int = Field(gt=0, le=1_000_000)
    limit_price: float = Field(gt=0, le=1_000_000)


class OrderPlaceRequest(BaseModel):
    preview_token: str = Field(min_length=20, max_length=8192)
    confirmation: str


class CancelOrderRequest(BaseModel):
    confirmation: str


def _error(exc: Exception):
    if isinstance(exc, LiveTradeRejected):
        raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(status_code=503, detail=str(exc))


@router.get("/status")
async def live_status(x_moomoo_read_token: str = Header(default="")):
    client = get_client()
    status = await asyncio.to_thread(client.status)
    import hmac
    granted = bool(client.settings.read_api_token and x_moomoo_read_token and
                   hmac.compare_digest(client.settings.read_api_token, x_moomoo_read_token))
    if not granted:
        status["account_id"] = None
    status["read_access_granted"] = granted
    return {"status": status, "policy": client.settings.public_policy(),
            "data_source": "Moomoo OpenD only", "paper_ledger_used": False}


@router.get("/snapshot")
async def live_snapshot(x_moomoo_read_token: str = Header(default="")):
    _require_read_token(x_moomoo_read_token)
    try:
        client = get_client()
        data = await asyncio.to_thread(client.snapshot)
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
        if not await asyncio.to_thread(is_module_order, order_id, get_client().settings.account_id):
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
