"""Expanded universe: NASDAQ-listed + S&P 500, with daily refresh + cache.

Sources:
  - nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt (official, updated daily)
  - en.wikipedia.org/wiki/List_of_S%26P_500_companies (via pandas.read_html)

Filters:
  - Drop test issues, ETFs (flagged in file), ADR suffixes like .W, .U, .R (warrants/units/rights)
  - Keep alphanumeric root tickers only
"""
from __future__ import annotations
import os, re, json, time, io
import urllib.request
from pathlib import Path

CACHE_DIR = Path(os.path.expanduser("~/trading-dashboard/data/universe"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
UNIVERSE_FILE = CACHE_DIR / "universe.json"
TTL_SECONDS = 24 * 3600

_EXCLUDE_SUFFIXES = re.compile(r"[\.\$]")  # drop tickers containing . or $
_BAD_NAME_PATTERNS = re.compile(
    r"\b(warrant|warrants|unit|units|right|rights|preferred|notes?|depositary|"
    r"subordinate|convertible)\b",
    re.IGNORECASE,
)


def _fetch_nasdaq_listed() -> list[str]:
    url = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    txt = urllib.request.urlopen(req, timeout=30).read().decode()
    out = []
    for line in txt.split("\n"):
        if not line or line.startswith("Symbol") or line.startswith("File Creation"):
            continue
        parts = line.split("|")
        if len(parts) < 7: continue
        sym, name, _cat, test_issue, _fin_status, _round_lot, etf = parts[:7]
        if test_issue == "Y": continue
        if etf == "Y": continue
        if _EXCLUDE_SUFFIXES.search(sym): continue
        if not sym or not sym.isalpha(): continue
        if _BAD_NAME_PATTERNS.search(name or ""): continue
        out.append(sym)
    return out


def _fetch_sp500() -> list[str]:
    """Fetch S&P 500 constituents. Wikipedia blocks urllib default UA;
    set one explicitly."""
    import pandas as pd
    import io
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30).read().decode()
    tables = pd.read_html(io.StringIO(html))
    df = tables[0]
    col = "Symbol" if "Symbol" in df.columns else df.columns[0]
    syms = [str(s).replace(".", "-") for s in df[col].tolist()]
    return [s for s in syms if s.replace("-", "").isalnum()]


def load_universe(force_refresh: bool = False) -> list[str]:
    """Return merged, sorted, de-duped universe. Cached to disk for TTL_SECONDS."""
    if not force_refresh and UNIVERSE_FILE.exists():
        try:
            meta = json.loads(UNIVERSE_FILE.read_text())
            if time.time() - meta.get("fetched_at", 0) < TTL_SECONDS:
                return meta["tickers"]
        except Exception:
            pass
    nasdaq = set()
    sp500 = set()
    try: nasdaq = set(_fetch_nasdaq_listed())
    except Exception as e: print(f"[universe] nasdaq fetch failed: {e}")
    try: sp500 = set(_fetch_sp500())
    except Exception as e: print(f"[universe] sp500 fetch failed: {e}")
    merged = sorted(nasdaq | sp500)
    if not merged and UNIVERSE_FILE.exists():
        # fall back to stale cache
        return json.loads(UNIVERSE_FILE.read_text())["tickers"]
    UNIVERSE_FILE.write_text(json.dumps({
        "fetched_at": time.time(),
        "nasdaq_count": len(nasdaq),
        "sp500_count": len(sp500),
        "total": len(merged),
        "tickers": merged,
    }))
    return merged


# -- Liquidity ranking -------------------------------------------------------
# Cache top-N by 60-day average dollar volume so 30-day backtests don't have to
# shove 600 pump-dump smallcaps through yfinance every single run.
LIQUIDITY_FILE = CACHE_DIR / "liquidity_ranking.json"
LIQUIDITY_TTL = 24 * 3600


def _compute_liquidity_ranking(universe: list[str], lookback_days: int = 60) -> list[dict]:
    """Rank tickers by avg(close * volume) over recent 1d bars.
    Uses the shared price_cache so we reuse whatever's already on disk."""
    from core.price_cache import get_history
    from datetime import datetime, timedelta
    end = datetime.utcnow().date()
    start = end - timedelta(days=lookback_days + 10)
    data = get_history(universe, start.isoformat(), end.isoformat(),
                       interval="1d", min_rows=20)
    ranked = []
    for t, df in data.items():
        if df is None or df.empty: continue
        adv = float((df["close"] * df["volume"]).tail(lookback_days).mean())
        if adv > 0:
            ranked.append({"ticker": t, "adv": adv})
    ranked.sort(key=lambda r: r["adv"], reverse=True)
    return ranked


def load_liquid_universe(top_n: int = 100, force_refresh: bool = False,
                         market: str = 'US') -> list[str]:
    """Return the top-N most-liquid tickers (by 60d avg dollar volume).

    market='US': merged NASDAQ+S&P500 ranked by ADV, cached 24h.
    market='CN': returns the full CSI300 universe from
                 ~/quant-trading/config/settings.UNIVERSES['CN']
                 (沪深300 已经是流动性精选池，无需再过滤). top_n is IGNORED
                 for CN — naive slicing biases toward Shenzhen because tickers
                 are sorted alphabetically and SZ codes (000xxx/002xxx/30xxxx)
                 sort before SH codes (60xxxx/68xxxx). Always return the full
                 list to preserve SH/SZ representation.
    """
    if (market or 'US').upper() == 'CN':
        import os, sys
        _qr = os.path.expanduser('~/quant-trading')
        if _qr not in sys.path:
            sys.path.insert(0, _qr)
        from config.settings import UNIVERSES
        return list(UNIVERSES.get('CN') or [])

    now = time.time()
    if not force_refresh and LIQUIDITY_FILE.exists():
        try:
            meta = json.loads(LIQUIDITY_FILE.read_text())
            if now - meta.get("fetched_at", 0) < LIQUIDITY_TTL:
                return [r["ticker"] for r in meta["ranking"][:top_n]]
        except Exception:
            pass
    universe = load_universe()
    ranking = _compute_liquidity_ranking(universe)
    if ranking:
        LIQUIDITY_FILE.write_text(json.dumps({
            "fetched_at": now,
            "universe_size": len(universe),
            "ranking": ranking,
        }))
    return [r["ticker"] for r in ranking[:top_n]]


if __name__ == "__main__":
    u = load_universe(force_refresh=True)
    print(f"Universe size: {len(u)}; sample: {u[:10]}")
