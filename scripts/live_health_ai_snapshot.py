#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.live_health_watchdog import diagnose

print(json.dumps(diagnose(mutate=False), ensure_ascii=False, sort_keys=True))