from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest

import api.live_account as live_api
from core.live_strategy_control import ControlRejected, LiveStrategyStore
from core.moomoo_client import MoomooClient, MoomooSettings, MoomooUnavailable
from server import app


def setup_client(tmp_path, monkeypatch):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    settings = MoomooSettings(read_api_token="r", control_api_token="c",
                              account_mode="DEDICATED", dedicated_account_confirmed=True)
    client = MoomooClient(settings=settings, control_store=store)
    monkeypatch.setattr(live_api, "_client", client)
    monkeypatch.setattr(live_api, "unresolved_preview_count", lambda: 0)
    return TestClient(app), store


def record_current_proof(client, store):
    fingerprint = client.current_sync_fingerprint()
    store.record_broker_sync_proof(fingerprint, store.snapshot().last_sync_at)


def test_control_read_requires_separate_read_token(tmp_path, monkeypatch):
    http, _ = setup_client(tmp_path, monkeypatch)
    assert http.get("/api/live-account/control").status_code == 401
    result = http.get("/api/live-account/control",
                      headers={"X-Moomoo-Read-Token": "r"})
    assert result.status_code == 200
    body = result.json()
    assert body["hard_limits"] == {"initial_capital": 10_000, "exposure_cap": 10_000,
                                   "loss_floor": 7_500, "regular_hours_only": True}
    assert body["state"]["lifecycle"] == "FROZEN"
    assert body["execution_status"]["status"] == "HELD"
    assert body["execution_status"]["active_holds"] == []
    assert body["execution_holds"] == []


def test_public_strategy_view_has_no_broker_account_data(tmp_path, monkeypatch):
    http, store = setup_client(tmp_path, monkeypatch)
    monkeypatch.setattr(live_api.get_client(), "quote", lambda code: {
        "market_state": "CLOSED", "sec_status": "NORMAL", "update_time": "2026-08-27 03:00:00",
    })
    store.apply_fill("strategy-fill", "US.DRAM", "BUY", 1, 58.21, 0.99)
    result = http.get("/api/live-account/strategy")
    assert result.status_code == 200
    body = result.json()
    assert body["data_scope"] == "strategy_subledger_only"
    assert body["execution_summary"]["total_trades"] == 1
    assert body["execution_summary"]["total_fees"] == 0.99
    assert body["market_status"] == {
        "state": "CLOSED", "security_status": "NORMAL", "updated_at": "2026-08-27 03:00:00",
    }
    assert "last_price" not in body["market_status"]
    assert body["performance_summary"]["sharpe_ratio"] is None
    assert body["owned_positions"][0]["symbol"] == "US.DRAM"
    assert body["symbol_performance"][0]["symbol"] == "US.DRAM"
    assert body["symbol_performance"][0]["holding"] is True
    assert body["symbol_performance"][0]["total_pnl"] == pytest.approx(-0.99)
    assert set(body["fills"][0]) == {
        "symbol", "side", "quantity", "price", "fee", "effective_fee",
        "fee_finalized", "applied_at",
    }
    assert all(set(event) == {"ts", "event_type", "source", "severity", "message"}
               for event in body["events"])
    for forbidden in ("account", "account_id", "positions", "orders", "deals", "order_fees"):
        assert forbidden not in body

    streamed = asyncio.run(live_api._live_strategy_payload(include_history=False))
    assert "equity" not in streamed
    assert "paper_series" not in streamed
    assert streamed["data_scope"] == "strategy_subledger_only"


def test_strategy_sse_response_is_unbuffered_and_snapshot_is_single_line():
    class Disconnected:
        async def is_disconnected(self):
            return True

    response = asyncio.run(live_api.live_strategy_stream(Disconnected()))  # type: ignore[arg-type]
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    event = live_api._sse_snapshot({"value": "line one\nline two"})
    assert event.startswith("event: snapshot\ndata: ")
    assert event.endswith("\n\n")
    assert event.count("\ndata: ") == 1
    assert "line one\\nline two" in event


def test_public_strategy_defense_in_depth_sanitizes_legacy_event_rows(tmp_path, monkeypatch):
    http, store = setup_client(tmp_path, monkeypatch)
    monkeypatch.setattr(live_api.get_client(), "quote", lambda _code: {})
    with store.connect() as con:
        con.execute(
            "INSERT INTO strategy_events(ts,event_type,source,severity,message,details_json,config_version) "
            "VALUES(?,?,?,?,?,?,?)",
            ("2026-08-28T00:00:00+00:00", "legacy", "test", "critical",
             "order alphaidentifier Authorization: Bearer LegacyBearer",
             json.dumps({"Authorization": "Digest nonce=LegacyNonce", "deal": "DealToken"}), 1),
        )
    body = http.get("/api/live-account/strategy").json()
    serialized = json.dumps(body, sort_keys=True)
    assert all(marker not in serialized for marker in (
        "alphaidentifier", "LegacyBearer", "LegacyNonce", "DealToken",
    ))


def test_live_api_exceptions_return_only_enumerated_public_errors(tmp_path, monkeypatch):
    http, store = setup_client(tmp_path, monkeypatch)
    marker = "Authorization: Bearer APISecret order alphaidentifier"
    monkeypatch.setattr(live_api.get_client(), "quote", lambda _code: (_ for _ in ()).throw(
        MoomooUnavailable(marker)
    ))
    quote = http.get("/api/live-account/quote/US.SPY", headers={"X-Moomoo-Read-Token": "r"})
    assert quote.status_code == 503
    assert quote.json()["detail"]["code"] == "MOOMOO_UNAVAILABLE"
    assert marker not in json.dumps(quote.json())

    monkeypatch.setattr(store, "config", lambda: (_ for _ in ()).throw(ControlRejected(marker)))
    control = http.get("/api/live-account/control", headers={"X-Moomoo-Read-Token": "r"})
    assert control.status_code == 503
    assert control.json()["detail"]["code"] == "CONTROL_STATE_UNAVAILABLE"
    assert marker not in json.dumps(control.json())


def test_preview_api_forwards_explicit_overnight_session(tmp_path, monkeypatch):
    http, _ = setup_client(tmp_path, monkeypatch)
    seen = {}

    def fake_preview(**payload):
        seen.update(payload)
        return {**payload, "preview_token": "x" * 32}

    monkeypatch.setattr(live_api.get_client(), "preview_order", fake_preview)
    result = http.post(
        "/api/live-account/orders/preview",
        headers={"X-Moomoo-Read-Token": "r"},
        json={"code": "DRAM", "side": "BUY", "qty": 1,
              "limit_price": 58.5, "session": "OVERNIGHT"},
    )
    assert result.status_code == 200
    assert seen["session"] == "OVERNIGHT"


def test_cors_rejects_untrusted_origins_and_allows_dashboard_origin(tmp_path, monkeypatch):
    http, _ = setup_client(tmp_path, monkeypatch)
    preflight = {"Access-Control-Request-Method": "GET"}
    evil = http.options("/api/live-account/status",
                        headers={"Origin": "https://untrusted.invalid", **preflight})
    assert evil.headers.get("access-control-allow-origin") is None
    trusted = http.options("/api/live-account/status",
                           headers={"Origin": "https://www.gexinhub.com", **preflight})
    assert trusted.headers.get("access-control-allow-origin") == "https://www.gexinhub.com"


def test_config_hot_reload_requires_control_token_and_version(tmp_path, monkeypatch):
    http, store = setup_client(tmp_path, monkeypatch)
    version = store.config()["version"]
    payload = {"expected_version": version,
               "patch": {"top_n": 5, "position_target_pct": 0.17,
                         "stop_cooldown_hours": 48},
               "reason": "paper candidate promotion"}
    assert http.put("/api/live-account/control/config", json=payload).status_code == 401
    result = http.put("/api/live-account/control/config", json=payload,
                      headers={"X-Moomoo-Control-Token": "c"})
    assert result.status_code == 200
    assert result.json()["version"] == version + 1
    assert store.config()["values"]["stop_cooldown_hours"] == 48
    stale = http.put("/api/live-account/control/config", json=payload,
                     headers={"X-Moomoo-Control-Token": "c"})
    assert stale.status_code == 409


def test_one_click_freeze_and_guarded_unfreeze(tmp_path, monkeypatch):
    http, store = setup_client(tmp_path, monkeypatch)
    with store.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                    "freeze_reason=NULL,last_sync_at=? WHERE id=1",
                    (datetime.now(timezone.utc).isoformat(),))
    headers = {"X-Moomoo-Control-Token": "c"}
    frozen = http.post("/api/live-account/control/freeze", headers=headers,
                       json={"confirmation": "FREEZE LIVE TRADING", "reason": "user switch"})
    assert frozen.status_code == 200
    assert store.snapshot().frozen
    assert frozen.json()["cancellation"]["attempted"] is False
    wrong = http.post("/api/live-account/control/unfreeze", headers=headers,
                      json={"confirmation": "wrong", "reason": "operator review complete"})
    assert wrong.status_code == 400
    store.mark_to_market({}, sync_complete=True)
    record_current_proof(live_api.get_client(), store)
    active = http.post("/api/live-account/control/unfreeze", headers=headers,
                       json={"confirmation": "UNFREEZE LIVE TRADING",
                             "reason": "operator review complete"})
    assert active.status_code == 200
    assert store.snapshot().lifecycle == "ACTIVE"


def test_freeze_api_and_public_strategy_never_return_operator_reason_text(tmp_path, monkeypatch):
    http, store = setup_client(tmp_path, monkeypatch)
    monkeypatch.setattr(live_api.get_client(), "quote", lambda _code: {})
    malicious = "order ORD12 Authorization: Basic NATURALSECRETONLYLETTERS"

    frozen = http.post(
        "/api/live-account/control/freeze",
        headers={"X-Moomoo-Control-Token": "c"},
        json={"confirmation": "FREEZE LIVE TRADING", "reason": malicious},
    )
    public = http.get("/api/live-account/strategy")

    assert frozen.status_code == 200
    assert public.status_code == 200
    serialized = json.dumps({"freeze": frozen.json(), "public": public.json()})
    assert "ORD12" not in serialized
    assert "NATURALSECRETONLYLETTERS" not in serialized
    assert frozen.json()["state"]["freeze_reason"] == "operator_requested_freeze"
    assert public.json()["state"]["freeze_reason"] == "operator_requested_freeze"
    assert store.snapshot().freeze_reason == "operator_requested_freeze"


def test_unfreeze_rejects_unknown_broker_outcome(tmp_path, monkeypatch):
    http, store = setup_client(tmp_path, monkeypatch)
    with store.connect() as con:
        con.execute("UPDATE strategy_state SET last_sync_at=? WHERE id=1",
                    (datetime.now(timezone.utc).isoformat(),))
    monkeypatch.setattr(live_api, "unresolved_preview_count", lambda: 1)
    result = http.post("/api/live-account/control/unfreeze",
                       headers={"X-Moomoo-Control-Token": "c"},
                       json={"confirmation": "UNFREEZE LIVE TRADING",
                             "reason": "operator review complete"})
    assert result.status_code == 409
    assert store.snapshot().lifecycle == "FROZEN"


def test_unfreeze_rejects_shared_account_even_after_fresh_read_only_sync(tmp_path, monkeypatch):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    settings = MoomooSettings(read_api_token="r", control_api_token="c",
                              dedicated_account_confirmed=False)
    client = MoomooClient(settings=settings, control_store=store)
    monkeypatch.setattr(live_api, "_client", client)
    monkeypatch.setattr(live_api, "unresolved_preview_count", lambda: 0)
    store.mark_to_market({}, sync_complete=True)

    result = TestClient(app).post(
        "/api/live-account/control/unfreeze",
        headers={"X-Moomoo-Control-Token": "c"},
        json={"confirmation": "UNFREEZE LIVE TRADING",
              "reason": "shared-account read-only sync complete"},
    )

    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "UNFREEZE_REJECTED"
    assert store.snapshot().lifecycle == "FROZEN"


def test_unfreeze_allows_explicit_shared_risk_acceptance_after_fresh_sync(tmp_path, monkeypatch):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    settings = MoomooSettings(read_api_token="r", control_api_token="c",
                              account_mode="SHARED_RESTRICTED",
                              shared_account_risk_accepted=True)
    client = MoomooClient(settings=settings, control_store=store)
    monkeypatch.setattr(live_api, "_client", client)
    monkeypatch.setattr(live_api, "unresolved_preview_count", lambda: 0)
    store.mark_to_market({}, sync_complete=True)
    record_current_proof(client, store)

    result = TestClient(app).post(
        "/api/live-account/control/unfreeze",
        headers={"X-Moomoo-Control-Token": "c"},
        json={"confirmation": "UNFREEZE LIVE TRADING",
              "reason": "restricted shared-account review complete"},
    )

    assert result.status_code == 200
    assert store.snapshot().lifecycle == "ACTIVE"


def test_cleanup_fails_closed_when_strategy_owns_shares(tmp_path, monkeypatch):
    http, store = setup_client(tmp_path, monkeypatch)
    with store.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                    "freeze_reason=NULL,last_sync_at='synced' WHERE id=1")
    store.apply_fill("runtime-fill", "US.AAPL", "BUY", 1, 100)
    result = http.post("/api/live-account/control/cleanup",
                       headers={"X-Moomoo-Control-Token": "c"},
                       json={"confirmation": "FREEZE ARCHIVE AND CLEAN STRATEGY",
                             "reason": "candidate failed"})
    assert result.status_code == 409
    assert store.snapshot().lifecycle == "FROZEN"
    assert store.snapshot().strategy_id == "B16"


def test_cleanup_rejects_active_broker_module_order(tmp_path, monkeypatch):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    settings = MoomooSettings(account_id=1, control_api_token="c")
    client = MoomooClient(settings=settings, control_store=store)
    client.snapshot = lambda: {
        "activity_warnings": [],
        "orders": [{"order_id": "runtime", "order_status": "SUBMITTED",
                    "remark": "dashboard:B16:runtime"}],
    }
    monkeypatch.setattr(live_api, "_client", client)
    monkeypatch.setattr(live_api, "unresolved_preview_count", lambda: 0)
    monkeypatch.setattr(live_api, "known_module_order_ids", lambda *_: set())
    result = TestClient(app).post(
        "/api/live-account/control/cleanup",
        headers={"X-Moomoo-Control-Token": "c"},
        json={"confirmation": "FREEZE ARCHIVE AND CLEAN STRATEGY",
              "reason": "candidate failed"},
    )
    assert result.status_code == 409
    assert store.snapshot().strategy_id == "B16"


def test_cleanup_rejects_when_broker_account_is_not_configured(tmp_path, monkeypatch):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    client = MoomooClient(
        settings=MoomooSettings(account_id=0, control_api_token="c"),
        control_store=store,
    )
    monkeypatch.setattr(live_api, "_client", client)
    monkeypatch.setattr(live_api, "unresolved_preview_count", lambda: 0)
    result = TestClient(app).post(
        "/api/live-account/control/cleanup",
        headers={"X-Moomoo-Control-Token": "c"},
        json={"confirmation": "FREEZE ARCHIVE AND CLEAN STRATEGY",
              "reason": "candidate failed"},
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "CLEANUP_REJECTED"
    assert store.snapshot().strategy_id == "B16"
