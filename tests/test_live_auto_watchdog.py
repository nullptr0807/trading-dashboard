from datetime import datetime, timedelta, timezone

from scripts.live_health_watchdog import auto_intent_health_problems


def test_auto_intent_health_thresholds_are_fail_closed():
    now = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(minutes=4)).isoformat()
    stale_dispatch = (now - timedelta(minutes=6)).isoformat()
    stale_ack = (now - timedelta(minutes=21)).isoformat()
    problems = auto_intent_health_problems([
        {"status": "DISPATCHING", "updated_at": fresh},
        {"status": "DISPATCHING", "updated_at": stale_dispatch},
        {"status": "UNKNOWN", "updated_at": fresh},
        {"status": "ACKED", "updated_at": stale_ack},
        {"status": "FILLED", "updated_at": stale_ack},
    ], now)
    assert problems == [
        "AUTO_INTENT_DISPATCH_STALE",
        "AUTO_INTENT_OUTCOME_UNKNOWN",
        "AUTO_INTENT_STALE:ACKED",
    ]
