import asyncio
import sqlite3
from pathlib import Path


def test_event_projection_marks_retired_by_account_and_market(tmp_path: Path):
    import api.events as events

    con = sqlite3.connect(tmp_path / 'events.db')
    con.executescript('''
        CREATE TABLE events(
            id INTEGER PRIMARY KEY, ts TEXT, category TEXT, severity TEXT,
            account TEXT, ticker TEXT, title TEXT, detail TEXT, market TEXT
        );
        CREATE TABLE account_meta(
            account_id TEXT, market TEXT, status TEXT,
            PRIMARY KEY(account_id, market)
        );
    ''')
    con.executemany(
        'INSERT INTO account_meta VALUES(?,?,?)',
        [('Q02', 'US', 'retired'), ('Q02', 'CN', 'active'), ('Q03', 'US', 'active')],
    )
    con.executemany(
        'INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)',
        [
            (1, '2026-09-01', 'factor', 'info', 'Q02', None, 'checkpoint Q02', None, 'US'),
            (2, '2026-09-01', 'factor', 'info', 'Q02', None, 'checkpoint Q02', None, 'CN'),
            (3, '2026-09-01', 'factor', 'info', 'Q03', None, 'checkpoint Q03', None, 'US'),
        ],
    )
    rows = con.execute(
        events._EVENT_SELECT + 'FROM events WHERE market = :m ORDER BY id',
        {'m': 'US'},
    ).fetchall()
    assert [(row[0], row[-1]) for row in rows] == [(1, 1), (3, 0)]


def test_events_api_exposes_retired_flag_for_frontend_filter(monkeypatch):
    import api.events as events

    captured = []

    async def fake_fetch(query, params=()):
        captured.append(query)
        return [{
            'id': 1, 'ts': '2026-09-01', 'category': 'factor', 'severity': 'info',
            'account': 'Q02', 'ticker': None, 'title': 'checkpoint Q02',
            'detail': None, 'retired': 1,
        }]

    monkeypatch.setattr(events, 'fetch_all', fake_fetch)
    monkeypatch.setattr(events, '_load_git_commits', lambda: [])
    payload = asyncio.run(events.list_events(limit=10, market='US'))
    assert payload['events'][0]['retired'] == 1
    assert all('_EVENT_SELECT' not in query for query in captured)
    assert all('account_meta' in query for query in captured)


def test_frontend_hides_retired_qlib_checkpoint_events():
    source = Path('static/js/events.js').read_text()
    assert "ev.retired" in source
    assert "/checkpoint/i" in source
    assert "^C?Q" in source
