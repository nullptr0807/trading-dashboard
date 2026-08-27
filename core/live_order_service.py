"""Shared audited dispatch path for manual API and automatic execution."""
from __future__ import annotations

from typing import Any, Protocol

from core.moomoo_audit import append_audit, finalize_preview
from core.moomoo_client import (
    BrokerOutcomeUnknown,
    LiveTradeRejected,
    MoomooUnavailable,
)


class OrderClient(Protocol):
    def verify_preview(self, token: str) -> dict[str, Any]: ...
    def authenticate_trade_token(self, provided: str) -> None: ...
    def place_order(self, preview_token: str, auth_token: str) -> dict[str, Any]: ...


def dispatch_signed_preview(
    client: OrderClient,
    preview_token: str,
    trade_token: str,
    *,
    source: str,
) -> dict[str, Any]:
    """Dispatch one signed preview with durable audit and outcome finalization.

    The caller must never retry ``BrokerOutcomeUnknown``. A durable attempt is
    written before the broker mutation. Tokens and signed preview blobs are not
    included in audit details.
    """
    preview: dict[str, Any] = {}
    try:
        preview = client.verify_preview(preview_token)
        client.authenticate_trade_token(trade_token)
        append_audit("place_attempt", False, {**preview, "source": source})
        result = client.place_order(preview_token, trade_token)
        order = result.get("order") or {}
        try:
            finalize_preview(preview["preview_id"], "accepted", str(order.get("order_id") or ""))
            append_audit(
                "place", True,
                {**preview, "order_id": order.get("order_id"), "source": source},
            )
            result["audit_status"] = "recorded"
        except Exception:
            result["audit_status"] = "pending_reconciliation"
        return result
    except BrokerOutcomeUnknown as exc:
        if preview.get("preview_id"):
            try:
                finalize_preview(preview["preview_id"], "unknown")
                append_audit(
                    "place_unknown", False,
                    {**preview, "error": str(exc), "source": source},
                )
            except Exception:
                pass
        raise
    except (MoomooUnavailable, LiveTradeRejected) as exc:
        if preview.get("preview_id"):
            try:
                finalize_preview(preview["preview_id"], "failed")
            except Exception:
                pass
        append_audit(
            "place", False,
            {**preview, "error": str(exc), "source": source},
        )
        raise
