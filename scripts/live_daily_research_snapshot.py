#!/usr/bin/env python3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.live_research_snapshot import main

sys.argv = [sys.argv[0], "--daily"]
raise SystemExit(main())