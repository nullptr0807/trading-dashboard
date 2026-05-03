"""Prefetch shim — data layer now lives in ~/quant-trading/data/trading.db.

This script used to maintain a separate parquet cache under
~/trading-dashboard/data/prices/. That fork was removed when we unified
the two projects on a single SQLite price cache.

To backfill prices now, run the quant-side script:

    cd ~/quant-trading && source venv/bin/activate
    python -m scripts.backfill_prices --all

Both the dashboard and the quant system will see the new rows automatically.
"""
import sys

MSG = __doc__

if __name__ == "__main__":
    print(MSG)
    sys.exit(0)
