#!/usr/bin/env python3
"""Safely validate or publish the current B16 live signal snapshot."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.live_signal_publication import (  # noqa: E402
    DEFAULT_FACTORS_PATH,
    DEFAULT_PUBLICATION_DB_PATH,
    DEFAULT_SOURCE_DB_PATH,
    PublicationError,
    publish_b16_signal,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate B16 source and append an immutable publication only with --publish",
    )
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB_PATH)
    parser.add_argument("--factors", type=Path, default=DEFAULT_FACTORS_PATH)
    parser.add_argument("--store", type=Path, default=DEFAULT_PUBLICATION_DB_PATH)
    parser.add_argument(
        "--publish", action="store_true",
        help="append after validation (default is dry-run and creates no store)",
    )
    args = parser.parse_args()
    try:
        result = publish_b16_signal(
            args.source_db, args.factors, args.store, publish=args.publish,
        )
    except PublicationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    output = asdict(result)
    output["mode"] = "published" if args.publish else "dry-run"
    output["ok"] = True
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
