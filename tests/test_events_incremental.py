import asyncio


def test_since_id_returns_only_new_market_db_events_in_incremental_order(monkeypatch):
    import api.events as events

    all_rows = [
        {"id": i, "ts": f"2026-09-01T00:00:{i:02d}+00:00", "category": "trade",
         "severity": "info", "account": "A01", "ticker": None,
         "title": f"db-{i}", "detail": None}
        for i in range(1, 9)
    ]
    seen_params = []

    async def fake_fetch(query, params=()):
        seen_params.append((query, dict(params)))
        rows = [row for row in all_rows if row["id"] > params["since_id"]]
        # Mirror the endpoint SQL: earliest unseen ids first prevents gaps at limit.
        return sorted(rows, key=lambda row: row["id"])[: params["lim"]]

    monkeypatch.setattr(events, "fetch_all", fake_fetch)
    monkeypatch.setattr(events, "_load_git_commits", lambda: [])

    first = asyncio.run(events.list_events(limit=3, market="CN", since_id=3))
    assert [event["id"] for event in first["events"]] == [6, 5, 4]
    assert all(event["id"] > 3 for event in first["events"])
    assert first["market"] == "CN"
    query, params = seen_params[0]
    assert "market = :m" in query and "id > :since_id" in query
    assert "ORDER BY id ASC" in query
    assert params["m"] == "CN" and params["since_id"] == 3 and params["lim"] == 3

    second = asyncio.run(events.list_events(limit=3, market="CN", since_id=max(e["id"] for e in first["events"])))
    assert [event["id"] for event in second["events"]] == [8, 7]
    assert not ({4, 5, 6} & {event["id"] for event in second["events"]})
