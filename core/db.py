import aiosqlite
from pathlib import Path

DB_PATH = Path.home() / 'quant-trading' / 'data' / 'trading.db'

async def fetch_all(query: str, params=()) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('PRAGMA query_only = ON')
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

async def fetch_one(query: str, params=()) -> dict | None:
    rows = await fetch_all(query, params)
    return rows[0] if rows else None
