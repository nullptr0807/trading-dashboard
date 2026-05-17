"""One-shot warmer: populate data/translations/industry_map.json for the full
Russell 1000 universe so /api/symbols/{tk}/peers returns immediately.

Run: ~/trading-dashboard/venv/bin/python scripts/warm_industry_map.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, '/home/gexin/quant-trading')
from config.settings import STOCK_UNIVERSE  # type: ignore

import yfinance as yf

OUT = Path('/home/gexin/trading-dashboard/data/translations/industry_map.json')
OUT.parent.mkdir(parents=True, exist_ok=True)
data: dict = json.loads(OUT.read_text()) if OUT.exists() else {}

todo = [tk for tk in STOCK_UNIVERSE if tk not in data]
print(f'Universe={len(STOCK_UNIVERSE)} cached={len(data)} todo={len(todo)}')

for i, tk in enumerate(todo, 1):
    try:
        info = yf.Ticker(tk).info or {}
        s, ind = info.get('sector'), info.get('industry')
        if s or ind:
            data[tk] = {'sector': s, 'industry': ind}
    except Exception as e:
        print(f'  ! {tk}: {e}')
    if i % 25 == 0 or i == len(todo):
        OUT.write_text(json.dumps(data, ensure_ascii=False))
        print(f'  [{i}/{len(todo)}] saved ({len(data)} total)')
    time.sleep(0.15)  # be polite

print(f'Done. {len(data)} tickers mapped → {OUT}')
