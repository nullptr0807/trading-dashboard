from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


_spec = importlib.util.spec_from_file_location(
    "live_health_watchdog_under_test",
    Path(__file__).resolve().parents[1] / "scripts" / "live_health_watchdog.py",
)
assert _spec and _spec.loader
watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog)


class FakeStore:
    def __init__(self):
        self.freeze_calls = 0
        self.holds = []
    def snapshot(self):
        return SimpleNamespace(
            lifecycle="FROZEN", freeze_reason="anomaly", strategy_equity=9000,
            loss_floor=7500, owned_market_value=1000, reserved_buy_notional=0,
            exposure_cap=10000, last_sync_at=None, config_version=1,
        )
    def freeze(self, *args, **kwargs):
        self.freeze_calls += 1
    def list_auto_order_intents(self, limit=1000):
        return []
    def list_execution_holds(self, active_only=False):
        return list(self.holds)
    def create_execution_hold(self, scope_type, scope_key, reason_code, source):
        hold = {"scope_type": scope_type, "scope_key": scope_key,
                "reason_code": reason_code, "source": source}
        if hold not in self.holds:
            self.holds.append(hold)
        return hold
    def resolve_execution_holds(self, **identity):
        before = len(self.holds)
        self.holds = [h for h in self.holds if not all(
            h.get(k) == identity.get(k) for k in ("scope_type", "scope_key", "reason_code", "source")
        )]
        return before - len(self.holds)
    def execution_status(self):
        return {"status": "HELD" if self.holds else "READY",
                "executable": not self.holds, "active_holds": list(self.holds),
                "unresolved_intent_count": len(self.list_auto_order_intents())}
    def event(self, *args, **kwargs):
        raise AssertionError("read-only AI snapshot must not write events")


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.settings = SimpleNamespace(account_id=0, trade_api_token="", password_md5="")


def test_ai_health_snapshot_cannot_freeze_or_cancel(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(watchdog, "LiveStrategyStore", lambda: store)
    monkeypatch.setattr(watchdog, "MoomooClient", FakeClient)
    monkeypatch.setattr(watchdog, "unknown_mutations", lambda: 0)
    result = watchdog.diagnose(mutate=False)
    assert result["healthy"] is True
    assert result["manual_freeze"] == {"active": True, "reason": "anomaly"}
    assert store.freeze_calls == 0
    assert "cancellation" not in result


def test_stale_acked_intent_creates_narrow_watchdog_hold_without_broker_mutation(monkeypatch):
    class ActiveStore(FakeStore):
        def __init__(self):
            super().__init__()
            self.freeze_reason = None

        def snapshot(self):
            return SimpleNamespace(
                lifecycle="ACTIVE", freeze_reason=None, strategy_equity=9000,
                loss_floor=7500, owned_market_value=1000, reserved_buy_notional=0,
                exposure_cap=10000, last_sync_at=datetime.now(timezone.utc).isoformat(),
                config_version=1,
            )

        def list_auto_order_intents(self, limit=1000):
            return [{
                "intent_id": "intent-1",
                "status": "ACKED",
                "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat(),
            }]

        def broker_sync_proof_matches(self, fingerprint):
            return True

        def freeze(self, reason, source):
            self.freeze_calls += 1
            self.freeze_reason = reason

    class ActiveClient(FakeClient):
        def current_sync_fingerprint(self):
            return "safe-test-fingerprint"

        def snapshot(self):
            return {"activity_warnings": [], "orders": []}

        def cancel_all_module_orders(self, token):
            raise AssertionError("credential-free watchdog test must not cancel")

    store = ActiveStore()
    monkeypatch.setattr(watchdog, "LiveStrategyStore", lambda: store)
    monkeypatch.setattr(watchdog, "MoomooClient", ActiveClient)
    monkeypatch.setattr(watchdog, "unknown_mutations", lambda: 0)

    result = watchdog.diagnose(mutate=True)

    assert result["healthy"] is False
    assert "AUTO_INTENT_STALE:ACKED" in result["problems"]
    assert store.freeze_calls == 0
    assert store.holds == [{
        "scope_type": "INTENT", "scope_key": "intent-1",
        "reason_code": "AUTO_INTENT_STALE_ACKED", "source": "health_watchdog",
    }]
    assert "cancellation" not in result
