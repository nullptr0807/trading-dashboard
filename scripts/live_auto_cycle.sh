#!/usr/bin/env bash
set -euo pipefail
MODE="${1:---shadow}"
case "$MODE" in --shadow|--execute) ;; *) echo "invalid mode" >&2; exit 2 ;; esac
if [ "$MODE" = "--execute" ]; then
  DOW=$(TZ=America/New_York date +%u)
  HM=$(TZ=America/New_York date +%H%M)
  [ "$DOW" -le 5 ] || exit 0
  case "$HM" in 0935|0950|1005|1020) ;; *) exit 0 ;; esac
fi
cd /home/gexin/trading-dashboard
source venv/bin/activate
set -a
source /home/gexin/.config/trading-dashboard/moomoo.env
set +a
export PYTHONPATH=.
exec python scripts/live_auto_cycle.py "$MODE" --quiet
