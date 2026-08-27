#!/usr/bin/env python3
"""Append a sanitized local strategy event from factor/signal/system jobs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.live_strategy_control import LiveStrategyStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--severity", choices=["debug", "info", "warning", "critical"], default="info")
    parser.add_argument("--message", required=True)
    parser.add_argument("--details-json", default="{}")
    args = parser.parse_args()
    details = json.loads(args.details_json)
    if not isinstance(details, dict):
        raise SystemExit("details-json must be an object")
    event_id = LiveStrategyStore().event(args.type, args.source, args.severity, args.message, details)
    print(json.dumps({"ok": True, "event_id": event_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
