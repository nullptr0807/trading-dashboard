from __future__ import annotations

import pytest

import core.live_order_service as service
from core.moomoo_client import BrokerOutcomeUnknown, LiveTradeRejected


class FakeClient:
    def __init__(self, outcome="ok"):
        self.outcome = outcome
        self.calls = []

    def verify_preview(self, token):
        self.calls.append("verify")
        return {"preview_id": "preview-1", "code": "US.TEST", "side": "BUY", "qty": 1}

    def authenticate_trade_token(self, token):
        self.calls.append("auth")
        if token != "trade-token":
            raise LiveTradeRejected("bad token")

    def place_order(self, preview_token, token):
        self.calls.append("broker")
        if self.outcome == "unknown":
            raise BrokerOutcomeUnknown("unknown")
        if self.outcome == "failed":
            raise LiveTradeRejected("rejected")
        return {"accepted": True, "order": {"order_id": "broker-order"}}


def wire_audit(monkeypatch, client):
    audit = []
    finalized = []

    def append(action, success, detail):
        audit.append((action, success, dict(detail)))
        if action == "place_attempt":
            client.calls.append("attempt")

    monkeypatch.setattr(service, "append_audit", append)
    monkeypatch.setattr(
        service, "finalize_preview",
        lambda preview_id, outcome, order_id=None: finalized.append((preview_id, outcome, order_id)),
    )
    return audit, finalized


def test_dispatch_records_attempt_before_broker_and_finalizes_success(monkeypatch):
    client = FakeClient()
    audit, finalized = wire_audit(monkeypatch, client)
    result = service.dispatch_signed_preview(
        client, "signed-preview", "trade-token", source="auto_executor",
    )
    assert client.calls == ["verify", "auth", "attempt", "broker"]
    assert finalized == [("preview-1", "accepted", "broker-order")]
    assert result["audit_status"] == "recorded"
    assert audit[-1][0:2] == ("place", True)
    assert audit[-1][2]["source"] == "auto_executor"
    assert "trade-token" not in repr(audit)
    assert "signed-preview" not in repr(audit)


def test_dispatch_unknown_finalizes_unknown_and_never_swallows(monkeypatch):
    client = FakeClient("unknown")
    audit, finalized = wire_audit(monkeypatch, client)
    with pytest.raises(BrokerOutcomeUnknown):
        service.dispatch_signed_preview(
            client, "signed-preview", "trade-token", source="auto_executor",
        )
    assert client.calls == ["verify", "auth", "attempt", "broker"]
    assert finalized == [("preview-1", "unknown", None)]
    assert audit[-1][0:2] == ("place_unknown", False)


def test_dispatch_known_rejection_finalizes_failed(monkeypatch):
    client = FakeClient("failed")
    audit, finalized = wire_audit(monkeypatch, client)
    with pytest.raises(LiveTradeRejected):
        service.dispatch_signed_preview(
            client, "signed-preview", "trade-token", source="manual_api",
        )
    assert finalized == [("preview-1", "failed", None)]
    assert audit[-1][0:2] == ("place", False)
