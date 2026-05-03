"""Live system events feed for the dashboard.

Sources merged into one stream (by ts DESC):
  1. The `events` table written by ~/quant-trading (data/factor/trade/risk/lifecycle/...)
  2. Synthetic `system` events: every git commit on this trading-dashboard repo
     becomes a "[系统]" event so the user sees code changes alongside live activity.

Pagination via `before_ts` for "load older". Polling on the client just re-pulls
the top page (no after_id) — newest events naturally rise to the top.
"""
import json
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from core.db import fetch_all

router = APIRouter(prefix='/api/events', tags=['events'])

VALID_MARKETS = {'US', 'CN'}
_REPO_DIR = Path(__file__).resolve().parent.parent  # /home/.../trading-dashboard
_GIT_CACHE_TTL = 30  # seconds
_git_cache: dict = {'ts': 0.0, 'commits': []}


def _validate_market(market: str) -> str:
    m = (market or 'US').upper()
    if m not in VALID_MARKETS:
        raise HTTPException(status_code=400, detail=f"invalid market '{market}'")
    return m


def _load_git_commits() -> list[dict]:
    """Return all commits as event-shaped dicts, newest first. Cached 30s."""
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
        sha, ts, author, subject = parts[0], parts[1], parts[2], parts[3]
        body = parts[4].strip() if len(parts) >= 5 else ''
        # Title only — no detail expanded into the stream (cleaner UI).
        title = f"🧬 {subject}"
        commits.append({
            'id': f'git_{sha[:12]}',
            'ts': ts,
            'category': 'system',
            'severity': 'info',
            'account': None,
            'ticker': None,
            'title': title,
            'detail': None,
        })
    _git_cache['ts'] = now
    _git_cache['commits'] = commits
    return commits


@router.get('')
async def list_events(
    limit: int = Query(100, ge=1, le=500),
    before_ts: str | None = Query(None, description="Return events strictly older than this ISO ts"),
    market: str = Query('US'),
):
    """Return events newest first.

    No `after_id` path anymore — clients poll by re-fetching the top page.
    For "load more older" use `before_ts=<oldest visible ts>`.
    `system` events (git commits) are market-agnostic and shown in every market.
    """
    market = _validate_market(market)

    # 1) DB events for this market (oversample so merging with git stays correct)
    over = limit * 2
    if before_ts:
        rows = await fetch_all(
            "SELECT id, ts, category, severity, account, ticker, title, detail "
            "FROM events WHERE market = :m AND ts < :bt "
            "ORDER BY ts DESC, id DESC LIMIT :lim",
            {'m': market, 'bt': before_ts, 'lim': over},
        )
    else:
        rows = await fetch_all(
            "SELECT id, ts, category, severity, account, ticker, title, detail "
            "FROM events WHERE market = :m ORDER BY ts DESC, id DESC LIMIT :lim",
            {'m': market, 'lim': over},
        )
    for r in rows:
        # detail stays as a raw string — client does its own JSON.parse() with try/catch.
        # (Some events have JSON detail, others have plain text like risk-regime banners.)
        pass

    # 2) Git commits (market-agnostic, full set is small)
    git = _load_git_commits()
    if before_ts:
        git = [g for g in git if g['ts'] < before_ts]

    # 3) Merge by ts DESC, cap to limit
    merged = sorted(rows + git, key=lambda e: (e['ts'] or '', str(e['id'])), reverse=True)
    merged = merged[:limit]
    return {'events': merged, 'count': len(merged), 'market': market}
