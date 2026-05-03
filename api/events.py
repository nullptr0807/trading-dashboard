"""Live system events feed for the dashboard."""
import json
from fastapi import APIRouter, Query, HTTPException

from core.db import fetch_all

router = APIRouter(prefix='/api/events', tags=['events'])

VALID_MARKETS = {'US', 'CN'}


def _validate_market(market: str) -> str:
    m = (market or 'US').upper()
    if m not in VALID_MARKETS:
        raise HTTPException(status_code=400, detail=f"invalid market '{market}'")
    return m


@router.get('')
async def list_events(
    limit: int = Query(50, ge=1, le=500),
    after_id: int | None = Query(None, description="Only return events with id > this"),
    market: str = Query('US'),
):
    """Return most recent events (filtered by market), newest first."""
    market = _validate_market(market)
    if after_id is not None:
        rows = await fetch_all(
            "SELECT id, ts, category, severity, account, ticker, title, detail "
            "FROM events WHERE id > :aid AND market = :m ORDER BY ts DESC, id DESC LIMIT :lim",
            {'aid': after_id, 'm': market, 'lim': limit},
        )
    else:
        rows = await fetch_all(
            "SELECT id, ts, category, severity, account, ticker, title, detail "
            "FROM events WHERE market = :m ORDER BY ts DESC, id DESC LIMIT :lim",
            {'m': market, 'lim': limit},
        )
    for r in rows:
        if r.get('detail'):
            try:
                r['detail'] = json.loads(r['detail'])
            except Exception:
                pass
    return {'events': rows, 'count': len(rows), 'market': market}
