#!/usr/bin/env python3
"""One fail-closed B16 live-auto cycle. Default mode is shadow."""
from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.live_auto_executor import AutoExecutionError, LiveAutoExecutor, auto_cycle_lock
from core.live_strategy_control import LiveStrategyStore
from core.moomoo_client import BrokerOutcomeUnknown, LiveTradeRejected, MoomooClient, MoomooUnavailable
from scripts.live_account_sync import reconcile


def main() -> int:
    parser = argparse.ArgumentParser(description="B16 independent live-subledger auto cycle")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--shadow", action="store_true", help="plan only; this is the default")
    mode.add_argument("--execute", action="store_true", help="allow one guarded live order")
    parser.add_argument("--quiet", action="store_true", help="stay silent for safe no-op ticks")
    args = parser.parse_args()
    store = LiveStrategyStore(read_only=not args.execute)
    client = MoomooClient(control_store=store)
    executor = LiveAutoExecutor(client, store, reconcile_fn=reconcile)
    try:
        with auto_cycle_lock():
            with redirect_stdout(io.StringIO()):
                result = executor.execute_one() if args.execute else executor.shadow()
    except BlockingIOError:
        return 0
    except AutoExecutionError:
        if not args.quiet:
            print(json.dumps({"mode": "execute" if args.execute else "shadow",
                              "status": "safety_gate_closed"}, sort_keys=True))
        return 0
    except BrokerOutcomeUnknown:
        print(json.dumps({"mode": "execute", "status": "broker_outcome_unknown_frozen"},
                         sort_keys=True))
        return 2
    except (LiveTradeRejected, MoomooUnavailable, RuntimeError):
        print(json.dumps({"mode": "execute" if args.execute else "shadow",
                          "status": "failed_closed"}, sort_keys=True))
        return 1
    if not args.quiet or result.get("status") not in {"no_action", "blocked_by_unresolved_intent"}:
        print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
