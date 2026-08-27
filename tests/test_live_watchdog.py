from __future__ import annotations

import importlib.util
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
    def snapshot(self):
        return SimpleNamespace(
            lifecycle="FROZEN", freeze_reason="anomaly", strategy_equity=9000,
            loss_floor=7500, owned_market_value=1000, reserved_buy_notional=0,
            exposure_cap=10000, last_sync_at=None, config_version=1,
        )
    def freeze(self, *args, **kwargs):
        self.freeze_calls += 1
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
    assert result["healthy"] is False
    assert store.freeze_calls == 0
    assert "cancellation" not in result
