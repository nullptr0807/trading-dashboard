import asyncio
import gzip
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient


def test_server_gzip_and_static_cache_contract(tmp_path, monkeypatch):
    import server

    client = TestClient(server.app)
    response = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert response.headers["cache-control"] == "no-cache"

    static = client.get("/static/js/trade.js?v=26", headers={"Accept-Encoding": "gzip"})
    assert static.headers["cache-control"] == "public, max-age=31536000, immutable"

    # httpx transparently expands the body, so the header is the compression proof.
    assert static.headers.get("content-encoding") == "gzip"


def test_live_sse_bypasses_gzip_and_flushes_chunks():
    from server import SelectiveGZipMiddleware

    messages = []

    async def stream_app(scope, receive, send):
        await send({
            'type': 'http.response.start', 'status': 200,
            'headers': [(b'content-type', b'text/event-stream')],
        })
        await send({'type': 'http.response.body', 'body': b'event: snapshot\n', 'more_body': True})
        await send({'type': 'http.response.body', 'body': b'data: {}\n\n', 'more_body': False})

    async def scenario():
        middleware = SelectiveGZipMiddleware(stream_app)

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            messages.append(message)

        await middleware({
            'type': 'http', 'method': 'GET',
            'path': '/api/live-account/strategy/stream',
            'headers': [(b'accept-encoding', b'gzip')],
        }, receive, send)

    asyncio.run(scenario())
    start = messages[0]
    assert not any(key.lower() == b'content-encoding' for key, _ in start['headers'])
    assert messages[1]['body'] == b'event: snapshot\n'
    assert messages[1]['more_body'] is True


def test_trade_cache_single_flight_and_stale_while_revalidate(monkeypatch):
    import api.trade as trade

    async def scenario():
        trade._API_CACHE.clear()
        trade._API_INFLIGHT.clear()
        calls = 0

        async def build():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return {"generation": calls}

        first = await asyncio.gather(*[
            trade._cached_api_value("summary", "US", 0.01, build, stale_ttl=1.0)
            for _ in range(8)
        ])
        assert calls == 1
        assert first == [{"generation": 1}] * 8

        await asyncio.sleep(0.02)
        stale = await trade._cached_api_value(
            "summary", "US", 0.01, build, stale_ttl=1.0
        )
        assert stale == {"generation": 1}
        await asyncio.sleep(0.04)
        assert calls == 2
        assert trade._cache_get("summary", "US", 1.0) == {"generation": 2}

    asyncio.run(scenario())


def test_account_detail_requests_are_single_flight(monkeypatch):
    import api.trade as trade

    async def scenario():
        trade._API_CACHE.clear()
        trade._API_INFLIGHT.clear()
        calls = 0

        async def build(account_id, market):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return {'account_id': account_id, 'market': market}

        monkeypatch.setattr(trade, '_build_account_detail', build)
        values = await asyncio.gather(*[
            trade.account_detail('B16', 'US') for _ in range(10)
        ])
        assert calls == 1
        assert values == [{'account_id': 'B16', 'market': 'US'}] * 10

    asyncio.run(scenario())


def test_equity_curves_share_a_bounded_timeline_with_benchmarks(monkeypatch):
    import api.trade as trade

    trade._API_CACHE.clear()
    trade._API_INFLIGHT.clear()
    total = trade.OVERVIEW_EQUITY_MAX_POINTS * 5
    from datetime import datetime, timedelta, timezone
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = [
        ("A1", 10_000.0 + i, (start + timedelta(hours=i)).isoformat(), 5_000.0)
        for i in range(total)
    ]

    async def fake_all(query, params=()):
        if "account_meta" in query:
            return [{"account_id": "A1", "status": "active", "retired_at": None,
                     "retire_reason": None, "initial_cash": 10_000.0}]
        return []

    async def fake_one(*_args, **_kwargs):
        return {"ts": rows[0][2]}

    async def fake_equity(*_args, **_kwargs):
        return rows

    benchmark_alignments = []

    async def fake_benchmark(ticker, since, initial=10_000.0, align_to=None):
        benchmark_alignments.append(list(align_to or []))
        # Deliberately mimic the old helper's leak of thousands of raw 5m bars.
        raw = [{"timestamp": f"raw-{i:05d}", "equity": initial + i} for i in range(4000)]
        return raw + [{"timestamp": ts, "equity": initial} for ts in (align_to or [])]

    monkeypatch.setattr(trade, "fetch_all", fake_all)
    monkeypatch.setattr(trade, "fetch_one", fake_one)
    monkeypatch.setattr(trade, "_fetch_account_equity_rows", fake_equity)
    monkeypatch.setattr(trade, "rebased_curve", fake_benchmark)
    monkeypatch.setattr(trade, "benchmarks_for", lambda _market: [{"ticker": "SPY", "label": "SPY"}])

    payload = asyncio.run(trade.equity_curves("US"))
    assert len(payload["curves"]["A1"]) <= trade.OVERVIEW_EQUITY_MAX_POINTS
    assert len(payload["curves"]["SPY"]) <= trade.OVERVIEW_EQUITY_MAX_POINTS
    assert len(benchmark_alignments[0]) <= trade.OVERVIEW_EQUITY_MAX_POINTS
    assert payload["curves"]["A1"][0]["equity"] == rows[0][1]
    assert payload["curves"]["A1"][-1]["equity"] == rows[-1][1]
    assert not any(p["timestamp"].startswith("raw-") for p in payload["curves"]["SPY"])


def test_live_chart_history_is_daily_close_and_stream_excludes_history(monkeypatch):
    import api.live_account as live

    rows = [
        {"ts": "2026-01-01T09:00:00Z", "equity": 100},
        {"ts": "2026-01-01T16:00:00Z", "equity": 110},
        {"ts": "2026-01-02T09:00:00Z", "equity": 120},
    ]
    assert live._daily_chart_close(rows) == [rows[1], rows[2]]

    class Store:
        def recent_events(self, _limit): return []
        def snapshot(self):
            from dataclasses import make_dataclass
            return make_dataclass("State", [])()
        def execution_status(self): return {}
        def list_execution_holds(self): return []
        def config(self): return {}
        def positions(self): return []
        def symbol_performance(self): return []
        def execution_summary(self): return {}
        def performance_summary(self): return {}
        def fill_display_history(self, _limit): return []
        def equity_history(self): return rows
        def paper_series(self): return [dict(x, series_id="p", label="P") for x in rows]

    class Client:
        control = Store()
        def quote(self, _code): return {}

    monkeypatch.setattr(live, "get_client", lambda: Client())
    full = asyncio.run(live._live_strategy_payload(include_history=True))
    incremental = asyncio.run(live._live_strategy_payload(include_history=False))
    assert full["equity"] == [rows[1], rows[2]]
    assert len(full["paper_series"]) == 2
    assert "equity" not in incremental and "paper_series" not in incremental


def test_factor_catalog_same_key_miss_is_single_flight(monkeypatch):
    import api.factor_lab as lab

    async def scenario():
        lab._CATALOG_CACHE.clear()
        lab._CATALOG_INFLIGHT.clear()
        calls = 0

        def build(market):
            nonlocal calls
            calls += 1
            import time
            time.sleep(0.03)
            return {"market": market}

        monkeypatch.setattr(lab, "_factor_lab_catalog_sync", build)
        results = await asyncio.gather(*[lab.factor_lab_catalog("US") for _ in range(6)])
        assert calls == 1
        assert results == [{"market": "US"}] * 6

    asyncio.run(scenario())


def test_signal_decay_loads_shared_base_once(monkeypatch):
    import api.signal_quality as quality

    calls = []

    def compute(account_id, market, horizons, window):
        calls.append((account_id, market, tuple(horizons), window))
        return {h: {"supported": True, "status": "ok", "summary": {"mean_ic": h},
                    "warnings": []} for h in horizons}

    monkeypatch.setattr(quality, "_compute_signal_quality_many_sync", compute)
    payload = asyncio.run(quality.signal_quality_decay("A1", "1,5,10,20", 20, "US"))
    assert calls == [("A1", "US", (1, 5, 10, 20), 20)]
    assert [row["summary"]["mean_ic"] for row in payload["decay"]] == [1, 5, 10, 20]
