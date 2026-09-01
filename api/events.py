"""Market-scoped live events merged with synthetic dashboard git events.

History pagination uses an opaque composite cursor over (timestamp, source,
source id). Polling may instead pass ``since_id`` for gap-free monotonic DB
increments; synthetic git events remain a small repeatable side stream.
"""
import base64
import json
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from core.db import fetch_all

router = APIRouter(prefix='/api/events', tags=['events'])
VALID_MARKETS = {'US', 'CN'}
_EVENT_SELECT = (
    "SELECT id, ts, category, severity, account, ticker, title, detail, "
    "EXISTS(SELECT 1 FROM account_meta m WHERE m.market = :m "
    "AND m.account_id = events.account AND m.status = 'retired') AS retired "
)
_REPO_DIR = Path(__file__).resolve().parent.parent
_GIT_CACHE_TTL = 30
_git_cache: dict = {'ts': 0.0, 'commits': []}


def _validate_market(market: str) -> str:
    m = (market or 'US').upper()
    if m not in VALID_MARKETS:
        raise HTTPException(status_code=400, detail=f"invalid market '{market}'")
    return m


def _load_git_commits() -> list[dict]:
    now = time.time()
    if now - _git_cache['ts'] < _GIT_CACHE_TTL and _git_cache['commits']:
        return _git_cache['commits']
    try:
        out = subprocess.run(
            ['git', '-C', str(_REPO_DIR), 'log',
             '--format=%H%x1f%cI%x1f%an%x1f%s%x1f%b%x1e', '-n', '500'],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    commits = []
    for chunk in out.split('\x1e'):
        chunk = chunk.strip('\n').strip()
        if not chunk:
            continue
        parts = chunk.split('\x1f')
        if len(parts) < 4:
            continue
        sha, ts, _author, subject = parts[:4]
        commits.append({
            'id': f'git_{sha[:12]}', 'ts': ts, 'category': 'system',
            'severity': 'info', 'account': None, 'ticker': None,
            'title': f"🧬 {subject}", 'detail': None,
        })
    _git_cache['ts'] = now
    _git_cache['commits'] = commits
    return commits


def _event_key(event: dict) -> tuple:
    source = event.get('_source') or ('git' if str(event.get('id', '')).startswith('git_') else 'db')
    if source == 'db':
        try:
            source_id = int(event['id'])
        except (TypeError, ValueError):
            source_id = 0
        return (event.get('ts') or '', 1, source_id)
    return (event.get('ts') or '', 0, str(event.get('id') or ''))


def _encode_cursor(event: dict) -> str:
    ts, source_rank, source_id = _event_key(event)
    payload = {'v': 1, 'ts': ts, 'source': 'db' if source_rank else 'git', 'id': source_id}
    raw = json.dumps(payload, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _decode_cursor(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
        payload = json.loads(raw)
        if payload.get('v') != 1 or payload.get('source') not in {'db', 'git'} or not isinstance(payload.get('ts'), str):
            raise ValueError
        if payload['source'] == 'db':
            payload['id'] = int(payload['id'])
        else:
            payload['id'] = str(payload['id'])
        return payload
    except Exception as exc:
        raise HTTPException(status_code=400, detail='invalid events cursor') from exc


def _cursor_key(cursor: dict) -> tuple:
    return (cursor['ts'], 1 if cursor['source'] == 'db' else 0, cursor['id'])


@router.get('')
async def list_events(
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None, description='Opaque composite cursor returned by next_cursor'),
    before_ts: str | None = Query(None, description='Deprecated strict timestamp cursor'),
    since_id: int | None = Query(None, ge=0, description='Return DB events with id greater than this value'),
    market: str = Query('US'),
):
    market = _validate_market(market)
    cursor = cursor if isinstance(cursor, str) else None
    before_ts = before_ts if isinstance(before_ts, str) else None
    since_id = since_id if isinstance(since_id, int) else None
    if since_id is not None and (cursor or before_ts):
        raise HTTPException(status_code=400, detail='since_id cannot be combined with cursor or before_ts')
    decoded = _decode_cursor(cursor)
    git = [dict(g, _source='git') for g in _load_git_commits()]

    if since_id is not None:
        # Select the earliest unseen ids first. If more than `limit` arrived
        # between polls, advancing to the largest returned id remains gap-free.
        rows = await fetch_all(
            _EVENT_SELECT +
            "FROM events WHERE market = :m AND id > :since_id "
            "ORDER BY id ASC LIMIT :lim",
            {'m': market, 'since_id': since_id, 'lim': limit},
        )
        db = [dict(r, _source='db') for r in rows]
        # Synthetic git events have no monotonic integer id. Merge a small set
        # only when DB events leave room; clients may receive these again.
        remaining = max(0, limit - len(db))
        merged = sorted(db + git[:remaining], key=_event_key, reverse=True)
        public = [{k: v for k, v in event.items() if k != '_source'} for event in merged]
        return {
            'events': public,
            'count': len(public),
            'market': market,
            'next_cursor': None,
        }

    # Fetch enough DB candidates to survive merging with the complete (max 500)
    # synthetic stream without hiding DB rows that precede the page boundary.
    db_limit = limit + len(git)
    if decoded:
        include_same_ts = 1 if decoded['source'] == 'db' else 0
        rows = await fetch_all(
            _EVENT_SELECT +
            "FROM events WHERE market = :m AND "
            "(ts < :bt OR (:include_same_ts = 1 AND ts = :bt AND id < :cid)) "
            "ORDER BY ts DESC, id DESC LIMIT :lim",
            {'m': market, 'bt': decoded['ts'], 'cid': decoded['id'] if include_same_ts else 0,
             'include_same_ts': include_same_ts, 'lim': db_limit},
        )
    elif before_ts:
        rows = await fetch_all(
            _EVENT_SELECT +
            "FROM events WHERE market = :m AND ts < :bt "
            "ORDER BY ts DESC, id DESC LIMIT :lim",
            {'m': market, 'bt': before_ts, 'lim': db_limit},
        )
    else:
        rows = await fetch_all(
            _EVENT_SELECT +
            "FROM events WHERE market = :m ORDER BY ts DESC, id DESC LIMIT :lim",
            {'m': market, 'lim': db_limit},
        )

    db = [dict(r, _source='db') for r in rows]
    if decoded:
        key = _cursor_key(decoded)
        git = [g for g in git if _event_key(g) < key]
    elif before_ts:
        git = [g for g in git if (g.get('ts') or '') < before_ts]

    merged = sorted(db + git, key=_event_key, reverse=True)[:limit]
    next_cursor = _encode_cursor(merged[-1]) if len(merged) == limit else None
    public = [{k: v for k, v in event.items() if k != '_source'} for event in merged]
    return {
        'events': public,
        'count': len(public),
        'market': market,
        'next_cursor': next_cursor,
    }
