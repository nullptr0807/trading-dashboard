#!/usr/bin/env python3
"""Produce a sanitized, read-only research packet for the sidecar agent."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.live_strategy_control import LiveStrategyStore


def market_phase(now: datetime) -> str:
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    minute = eastern.hour * 60 + eastern.minute
    if eastern.weekday() >= 5:
        return "CLOSED"
    if 570 <= minute < 960:
        return "REGULAR"
    if minute >= 960:
        return "POST_CLOSE"
    return "PRE_OPEN"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily", action="store_true")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    store = LiveStrategyStore()
    state = store.snapshot()
    events = store.recent_events(500 if args.daily else 100)
    paper = store.paper_series()
    packet = {
        "generated_at": now.isoformat(), "market_phase": market_phase(now),
        "report_type": "daily" if args.daily else "intraday_10m",
        "production_mutation_allowed": False,
        "state": state.__dict__, "config": store.config() if state.strategy_id else None,
        "owned_positions": store.positions(), "equity": store.equity_history(5000 if args.daily else 200),
        "events": events, "paper_candidates": paper,
        "instructions": {
            "candidate_only": True,
            "never_unfreeze": True,
            "never_edit_live_config": True,
            "prices_and_broker_truth": "Moomoo OpenD only",
            "external_holdings": "excluded",
        },
    }
    print(json.dumps(packet, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
