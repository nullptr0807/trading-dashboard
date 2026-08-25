import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1]))


def test_composite_cursor_returns_same_timestamp_db_and_git_events_exactly_once(monkeypatch):
    import api.events as events

    ts = '2026-08-25T12:00:00+00:00'
    db_events = [
        {'id': i, 'ts': ts, 'category': 'trade', 'severity': 'info', 'account': 'A1', 'ticker': None, 'title': f'db-{i}', 'detail': None}
        for i in range(1, 121)
    ]
    git_events = [
        {'id': f'git_{suffix}', 'ts': ts, 'category': 'system', 'severity': 'info', 'account': None, 'ticker': None, 'title': f'git-{suffix}', 'detail': None}
        for suffix in ('fff', 'aaa')
    ]

    async def fake_fetch(query, params=()):
        rows = list(db_events)
        bt = params.get('bt')
        cid = params.get('cid')
        include_same_ts = params.get('include_same_ts', 0)
        if bt:
            rows = [r for r in rows if r['ts'] < bt or (include_same_ts and r['ts'] == bt and r['id'] < cid)]
        rows.sort(key=lambda r: (r['ts'], r['id']), reverse=True)
        return rows[: params['lim']]

    monkeypatch.setattr(events, 'fetch_all', fake_fetch)
    monkeypatch.setattr(events, '_load_git_commits', lambda: list(git_events))

    got = []
    cursor = None
    for _ in range(20):
        page = asyncio.run(events.list_events(limit=17, cursor=cursor, market='US'))
        got.extend((str(e['id']), e['title']) for e in page['events'])
        cursor = page['next_cursor']
        if not cursor:
            break

    expected = {(str(e['id']), e['title']) for e in db_events + git_events}
    assert set(got) == expected
    assert len(got) == len(expected)
    assert len(got) == len(set(got))


def test_cursor_order_handles_numeric_ids_numerically(monkeypatch):
    import api.events as events

    ts = '2026-08-25T12:00:00+00:00'
    db_events = [
        {'id': i, 'ts': ts, 'category': 'data', 'severity': 'info', 'account': None, 'ticker': None, 'title': str(i), 'detail': None}
        for i in (2, 10, 100)
    ]

    async def fake_fetch(query, params=()):
        rows = db_events
        if params.get('bt') and params.get('include_same_ts'):
            rows = [r for r in rows if r['id'] < params['cid']]
        return sorted(rows, key=lambda r: r['id'], reverse=True)[:params['lim']]

    monkeypatch.setattr(events, 'fetch_all', fake_fetch)
    monkeypatch.setattr(events, '_load_git_commits', lambda: [])
    first = asyncio.run(events.list_events(limit=2, cursor=None, market='US'))
    second = asyncio.run(events.list_events(limit=2, cursor=first['next_cursor'], market='US'))
    assert [e['id'] for e in first['events'] + second['events']] == [100, 10, 2]
