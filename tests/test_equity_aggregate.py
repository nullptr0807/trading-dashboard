import asyncio
import json
from datetime import datetime, timedelta, timezone


def _points(offset, count=80):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [
        {"timestamp": (start + timedelta(hours=i)).isoformat(), "equity": 10_000 + offset + i}
        for i in range(count)
    ]


def test_aggregate_equity_contract_is_small_active_and_endpoint_preserving(monkeypatch):
    import api.trade as trade

    curves = {"SPY": _points(0), "QQQ": _points(10)}
    meta = {
        "SPY": {"status": "active"},
        "QQQ": {"status": "active"},
    }
    for group_index, group in enumerate("ABFQ"):
        for account_index in range(6):
            name = f"{group}{account_index + 1:02d}"
            curves[name] = _points(group_index * 100 + account_index)
            meta[name] = {"status": "active", "group": group}
        retired = f"{group}99"
        curves[retired] = _points(50_000)
        meta[retired] = {"status": "retired", "group": group}

    full = {"curves": curves, "meta": meta}
    monkeypatch.setattr(
        trade,
        "benchmarks_for",
        lambda market: [{"ticker": "SPY", "label": "SPY"}, {"ticker": "QQQ", "label": "QQQ"}],
    )
    aggregate = trade._aggregate_equity_curves(full, "US")

    assert aggregate["view"] == "aggregate"
    assert len(aggregate["curves"]) <= 7
    assert set(aggregate["curves"]) == {"A · MEDIAN", "B · MEDIAN", "F · MEDIAN", "Q · MEDIAN", "SPY", "QQQ"}
    assert all(aggregate["meta"][f"{group} · MEDIAN"]["aggregate"] for group in "ABFQ")
    # Retired 50k curves must not influence the active medians.
    assert aggregate["curves"]["A · MEDIAN"][0]["equity"] < 11_000
    assert len(json.dumps(aggregate, separators=(",", ":"))) < len(json.dumps(full, separators=(",", ":"))) * 0.35
    for points in aggregate["curves"].values():
        assert points[0]["timestamp"] == curves["SPY"][0]["timestamp"]
        assert points[-1]["timestamp"] == curves["SPY"][-1]["timestamp"]


def test_aggregate_and_full_views_share_one_cold_full_build(monkeypatch):
    import api.trade as trade

    async def scenario():
        trade._API_CACHE.clear()
        trade._API_INFLIGHT.clear()
        calls = 0

        async def build(_market):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return {
                "curves": {"A01": _points(0, 3), "SPY": _points(0, 3)},
                "meta": {"A01": {"status": "active", "group": "A"}, "SPY": {"status": "active"}},
            }

        monkeypatch.setattr(trade, "_build_equity_curves", build)
        monkeypatch.setattr(trade, "benchmarks_for", lambda market: [{"ticker": "SPY", "label": "SPY"}])
        full, aggregate, aggregate_again = await asyncio.gather(
            trade.equity_curves("US", "full"),
            trade.equity_curves("US", "aggregate"),
            trade.equity_curves("US", "aggregate"),
        )
        assert calls == 1
        assert full["curves"].keys() == {"A01", "SPY"}
        assert aggregate == aggregate_again
        assert aggregate["view"] == "aggregate"
        assert ("equity_curves:full", "US") in trade._API_CACHE
        assert ("equity_curves:aggregate", "US") in trade._API_CACHE

    asyncio.run(scenario())


def test_late_account_entry_cannot_create_aggregate_level_jump(monkeypatch):
    import api.trade as trade

    monkeypatch.setattr(trade, 'benchmarks_for', lambda _market: [])
    payload = {
        'curves': {
            'A01': [
                {'timestamp': '2026-01-01', 'equity': 100.0},
                {'timestamp': '2026-01-02', 'equity': 110.0},
                {'timestamp': '2026-01-03', 'equity': 121.0},
            ],
            # A02 starts later at a very different absolute level. Its first
            # observation must not reset or jump the aggregate curve.
            'A02': [
                {'timestamp': '2026-01-02', 'equity': 10_000.0},
                {'timestamp': '2026-01-03', 'equity': 11_000.0},
            ],
        },
        'meta': {
            'A01': {'status': 'active', 'group': 'A'},
            'A02': {'status': 'active', 'group': 'A'},
        },
    }
    aggregate = trade._aggregate_equity_curves(payload, 'US')
    points = aggregate['curves']['A · MEDIAN']
    assert [point['equity'] for point in points] == [100.0, 110.0, 121.0]
    assert aggregate['meta']['A · MEDIAN']['aggregation'] == 'chain_linked_median_return'
