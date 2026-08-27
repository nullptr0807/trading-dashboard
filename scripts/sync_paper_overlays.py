#!/usr/bin/env python3
"""Sync approved paper-account and parameter-candidate curves into live UI overlays."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.live_strategy_control import LiveStrategyStore

PAPER_DB = Path("/home/gexin/quant-trading/data/trading.db")
REFERENCE_ACCOUNTS = {
    "A02": "Paper A02 · recent IC reference",
    "A09": "Paper A09 · secondary IC reference",
}


def sync_accounts(store: LiveStrategyStore) -> int:
    con = sqlite3.connect(f"file:{PAPER_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    count = 0
    try:
        for account, label in REFERENCE_ACCOUNTS.items():
            rows = con.execute("""
                SELECT day,equity,timestamp FROM (
                    SELECT substr(timestamp,1,10) day,equity,timestamp,
                           row_number() OVER (
                               PARTITION BY substr(timestamp,1,10) ORDER BY timestamp DESC
                           ) rn
                    FROM accounts WHERE name=? AND market='US'
                ) WHERE rn=1 ORDER BY timestamp
            """, (account,)).fetchall()
            if not rows:
                continue
            baseline = float(rows[0]["equity"])
            for row in rows:
                normalized = 10_000 * float(row["equity"]) / baseline
                store.upsert_paper_point(
                    f"account-{account}", row["timestamp"], label, "account",
                    normalized, normalized / 10_000 - 1,
                    account_ref=account,
                    params={"source": "quant-trading paper ledger", "normalized_initial": 10_000},
                )
                count += 1
    finally:
        con.close()
    return count


def import_parameter_curve(store: LiveStrategyStore, path: Path) -> int:
    payload = json.loads(path.read_text())
    count = 0
    metadata = {**payload["params"], "metrics": payload.get("metrics"),
                "coverage": payload.get("coverage"), "research_only": True}
    for point in payload["curve"]:
        equity = float(point[1])
        store.upsert_paper_point(
            payload["series_id"], str(point[0]), payload["label"], "parameter",
            equity, equity / 10_000 - 1, params=metadata,
        )
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parameter-curve", type=Path, action="append", default=[])
    args = parser.parse_args()
    store = LiveStrategyStore()
    count = sync_accounts(store)
    for path in args.parameter_curve:
        count += import_parameter_curve(store, path)
    store.event("paper_overlay_sync", "paper_sync", "info",
                "Paper comparison curves updated", {"points": count})
    print(json.dumps({"ok": True, "points": count, "accounts": sorted(REFERENCE_ACCOUNTS)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
