from fastapi import APIRouter, Query, HTTPException
from core.db import fetch_all, fetch_one, DB_PATH
from core.benchmarks import rebased_curve, benchmarks_for
import os, json, sqlite3, asyncio, time, re, base64
from datetime import datetime, timezone
from functools import lru_cache

router = APIRouter(prefix='/api/trade', tags=['trade'])

# Short API caches smooth repeated dashboard loads while preserving near-live
# behaviour (price updater/cycles run at minute-ish cadence). Cold-path SQL is
# still correct; hot-path avoids re-scanning ~0.8M US account rows for every
# tab render / language switch / market toggle.
_API_CACHE: dict[tuple[str, str], tuple[float, object]] = {}
_API_INFLIGHT: dict[tuple[str, str], asyncio.Task] = {}


def _cache_get(kind: str, market: str, ttl: float):
    item = _API_CACHE.get((kind, market))
    if not item:
        return None
    ts, value = item
    if time.monotonic() - ts > ttl:
        return None
    return value


def _cache_set(kind: str, market: str, value):
    _API_CACHE[(kind, market)] = (time.monotonic(), value)
    return value


async def _cached_api_value(kind: str, market: str, ttl: float, builder,
                            *, stale_ttl: float = 0.0):
    """Coalesce misses and serve bounded stale data while one refresh runs."""
    key = (kind, market)
    now = time.monotonic()
    cached = _API_CACHE.get(key)
    if cached and now - cached[0] <= ttl:
        return cached[1]

    task = _API_INFLIGHT.get(key)
    if task is None:
        async def refresh():
            try:
                value = await builder()
                return _cache_set(kind, market, value)
            finally:
                _API_INFLIGHT.pop(key, None)

        task = asyncio.create_task(refresh())
        _API_INFLIGHT[key] = task

    if cached and now - cached[0] <= ttl + stale_ttl:
        # Retrieve any refresh exception so background SWR failures do not emit
        # "Task exception was never retrieved"; stale data remains available.
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return cached[1]
    return await asyncio.shield(task)


async def _singleflight_api_value(kind: str, market: str, builder):
    """Coalesce concurrent builds without retaining a stale response."""
    key = (kind, market)
    task = _API_INFLIGHT.get(key)
    if task is None:
        async def build_once():
            try:
                return await builder()
            finally:
                _API_INFLIGHT.pop(key, None)
        task = asyncio.create_task(build_once())
        _API_INFLIGHT[key] = task
    return await asyncio.shield(task)


VALID_MARKETS = {'US', 'CN'}
OVERVIEW_EQUITY_MAX_POINTS = 720
ACCOUNT_EQUITY_MAX_POINTS = 1200
ACCOUNT_BENCHMARK_MAX_POINTS = 600
ACCOUNT_SNAPSHOT_MAX = 240
ACCOUNT_HOLDINGS_PER_SNAPSHOT_MAX = 50
ACCOUNT_TRADES_MAX = 200
ACCOUNT_TRADE_PAGE_MAX = 500
ACCOUNT_TRADE_MARKERS_MAX = 1200


def _annualized_return_pct(initial_cash, equity, created_at, as_of):
    """Return inception-to-latest-mark CAGR as a percentage."""
    try:
        initial = float(initial_cash)
        final = float(equity)
        start = datetime.fromisoformat(str(created_at).replace('Z', '+00:00'))
        end = datetime.fromisoformat(str(as_of).replace('Z', '+00:00'))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        years = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / (365.25 * 86400)
        if initial <= 0 or final < 0 or years <= 0:
            return None
        if final == 0:
            return -100.0
        return ((final / initial) ** (1 / years) - 1) * 100
    except (TypeError, ValueError, OverflowError):
        return None


def _account_age_days(created_at, retired_at=None, now=None):
    """Elapsed whole calendar-equivalent days; freeze retired lifetimes."""
    try:
        start = datetime.fromisoformat(str(created_at).replace('Z', '+00:00'))
        end_value = retired_at or now or datetime.now(timezone.utc)
        end = end_value if isinstance(end_value, datetime) else datetime.fromisoformat(str(end_value).replace('Z', '+00:00'))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        seconds = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
        return max(0, int(seconds // 86400))
    except (TypeError, ValueError):
        return None

CN_UNIVERSE_FILE = os.path.expanduser('~/quant-trading/data/cn_universe.json')


@lru_cache(maxsize=1)
def _cn_ticker_names() -> dict:
    """Return {ticker: {'cn': name, 'en': name}} for CSI300 + index. Cached.

    Sourced from akshare via ~/quant-trading/data/cn_universe.json (refreshed by
    refresh_cn_universe.py). Returns empty dict if file missing or malformed.
    """
    try:
        with open(CN_UNIVERSE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get('names', {}) or {}
    except (OSError, ValueError):
        return {}


def _validate_market(market: str) -> str:
    m = (market or 'US').upper()
    if m not in VALID_MARKETS:
        raise HTTPException(status_code=400, detail=f"invalid market '{market}'; expected one of {sorted(VALID_MARKETS)}")
    return m


def _validate_account_id(account_id: str) -> str:
    value = str(account_id or '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise HTTPException(status_code=400, detail='invalid account id')
    return value


def _downsample_endpoints(rows: list[dict], maximum: int) -> list[dict]:
    """Uniformly bound a time series while retaining exact first/last points."""
    if len(rows) <= maximum:
        return rows
    if maximum <= 1:
        return rows[-1:]
    last = len(rows) - 1
    indexes = {round(i * last / (maximum - 1)) for i in range(maximum)}
    return [row for i, row in enumerate(rows) if i in indexes]


def _trade_cursor(timestamp: str, trade_id: int) -> str:
    raw = json.dumps({'v': 1, 'ts': timestamp, 'id': trade_id}, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _decode_trade_cursor(value: str | None) -> tuple[str, int] | None:
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
        data = json.loads(raw)
        if data.get('v') != 1 or not isinstance(data.get('ts'), str):
            raise ValueError
        return data['ts'], int(data['id'])
    except Exception as exc:
        raise HTTPException(status_code=400, detail='invalid trades cursor') from exc


def _fetch_account_equity_rows_sync(market: str, *, since_45d: bool = False) -> list[tuple[str, float, str, float]]:
    """Fetch deterministic bucket-close rows: hourly recent, daily older.

    A transient intrabucket V-spike no longer gets averaged into the whole bucket;
    only the final complete mark represents that bucket.
    """
    where_recent = "AND a.timestamp >= datetime('now', '-45 days')" if since_45d else ""
    if since_45d:
        # Sharpe only needs one stable observation per hour; SQL aggregation keeps
        # the account-list request cheap and is not used to render the curve.
        sql = f"""
            SELECT a.name,AVG(a.equity),MAX(a.timestamp),AVG(a.cash)
            FROM accounts a
            JOIN account_meta m ON m.account_id=a.name AND m.market=a.market
            WHERE a.market=? AND m.market=? {where_recent}
            GROUP BY a.name,substr(a.timestamp,1,13)
            ORDER BY a.name,MAX(a.timestamp)
        """
        params = (market, market)
    else:
        bucket = "substr(a.timestamp,1,13)"
        sql = f"""
            SELECT a.name,a.equity,MAX(a.timestamp),a.cash
            FROM accounts a
            JOIN account_meta m ON m.account_id=a.name AND m.market=a.market
            WHERE a.market=? AND m.market=?
            GROUP BY a.name,{bucket}
            ORDER BY a.name,MAX(a.timestamp)
        """
        params = (market, market)
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        con.execute('PRAGMA query_only = ON')
        con.execute('PRAGMA busy_timeout=30000')
        return [(str(n), float(e or 0), str(ts), float(c or 0)) for n, e, ts, c in con.execute(sql, params)]
    finally:
        con.close()


async def _fetch_account_equity_rows(market: str, *, since_45d: bool = False) -> list[tuple[str, float, str, float]]:
    return await asyncio.to_thread(_fetch_account_equity_rows_sync, market, since_45d=since_45d)


async def _build_summary(market: str):
    # Source of truth for market = account_meta.market (the `accounts` snapshot
    # table inherits the default 'US' for everything, so we always join through
    # account_meta to filter properly).
    rows = await fetch_all('''
        SELECT m.account_id AS name, a.cash, a.equity, a.timestamp,
               m."group", m.strategy_name, m.initial_cash, m.status,
               COALESCE(m.runtime_status,'ready') AS runtime_status,
               m.runtime_reason
        FROM account_meta m
        LEFT JOIN accounts a ON a.id = (
            SELECT x.id FROM accounts x
            WHERE x.market=m.market AND x.name=m.account_id
            ORDER BY x.timestamp DESC LIMIT 1
        )
        WHERE m.market = :market
    ''', {'market': market})

    # If `accounts` is empty for this market (e.g. CN before first cron tick),
    # fall back to account_state + account_meta so the dashboard still shows
    # all configured accounts at their initial equity.
    if not rows:
        meta_rows = await fetch_all(
            'SELECT account_id as name, "group", strategy_name, initial_cash, status, '
            "COALESCE(runtime_status,'ready') AS runtime_status, runtime_reason "
            'FROM account_meta WHERE market = :market', {'market': market}
        )
        state_rows = await fetch_all(
            'SELECT account, cash, initial_cash, updated_at as timestamp '
            'FROM account_state WHERE market = :market', {'market': market}
        )
        state_by_acc = {r['account']: r for r in state_rows}
        rows = []
        for m in meta_rows:
            st = state_by_acc.get(m['name'], {})
            cash = st.get('cash', m.get('initial_cash') or 10000)
            rows.append({
                'name': m['name'],
                'cash': cash,
                'equity': cash,  # no positions yet
                'timestamp': st.get('timestamp'),
                'group': m.get('group') or '',
                'strategy_name': m.get('strategy_name') or '',
                'initial_cash': m.get('initial_cash') or (100000 if market == 'CN' else 10000),
                'status': m.get('status') or 'active',
                'runtime_status': m.get('runtime_status') or 'ready',
                'runtime_reason': m.get('runtime_reason'),
            })

    if any(r.get('equity') is None for r in rows):
        state_rows = await fetch_all(
            'SELECT account,cash,initial_cash,updated_at AS timestamp '
            'FROM account_state WHERE market=:market', {'market': market}
        )
        state_by_acc = {r['account']: r for r in state_rows}
        for r in rows:
            if r.get('equity') is not None:
                continue
            state = state_by_acc.get(r['name'], {})
            initial = r.get('initial_cash') or (100000 if market == 'CN' else 10000)
            cash = state.get('cash') if state.get('cash') is not None else initial
            r['cash'] = cash
            r['equity'] = cash
            r['timestamp'] = state.get('timestamp')

    default_init = 100000.0 if market == 'CN' else 10000.0
    for r in rows:
        if not r.get('initial_cash'):
            r['initial_cash'] = default_init

    total_equity = sum(r['equity'] for r in rows)
    total_initial = sum(r['initial_cash'] for r in rows)
    total_pnl = total_equity - total_initial

    per_account = [
        {
            'account_id': r['name'],
            'group': r.get('group') or '',
            'strategy_name': r.get('strategy_name') or '',
            'status': r.get('status') or 'active',
            'runtime_status': r.get('runtime_status') or 'ready',
            'runtime_reason': r.get('runtime_reason'),
            'pnl': round(r['equity'] - r['initial_cash'], 2),
            'pnl_pct': round((r['equity'] - r['initial_cash']) / r['initial_cash'] * 100, 2),
        }
        for r in rows
    ]
    # Retired accounts are frozen — their final return is real (we keep it in
    # totals + per_account for display) but excluding them from distribution
    # stats avoids skewing median/IQR/win-rate/best/worst with stale figures.
    active_for_dist = [
        a for a in per_account
        if a.get('status') != 'retired' and a.get('runtime_status', 'ready') == 'ready'
    ]
    pcts = sorted(a['pnl_pct'] for a in active_for_dist)

    def _quantile(xs, q):
        if not xs:
            return 0.0
        k = (len(xs) - 1) * q
        f = int(k)
        c = min(f + 1, len(xs) - 1)
        if f == c:
            return xs[f]
        return xs[f] + (xs[c] - xs[f]) * (k - f)

    if active_for_dist:
        best = max(active_for_dist, key=lambda a: a['pnl_pct'])
        worst = min(active_for_dist, key=lambda a: a['pnl_pct'])
    else:
        best = worst = None
    distribution = {
        'count': len(pcts),
        'retired_count': sum(1 for a in per_account if a.get('status') == 'retired'),
        'non_tradeable_count': sum(
            1 for a in per_account
            if a.get('status') != 'retired' and a.get('runtime_status', 'ready') != 'ready'
        ),
        'best': best,
        'worst': worst,
        'median_pct': round(_quantile(pcts, 0.5), 2) if pcts else 0.0,
        'mean_pct': round(sum(pcts) / len(pcts), 2) if pcts else 0.0,
        'q1_pct': round(_quantile(pcts, 0.25), 2) if pcts else 0.0,
        'q3_pct': round(_quantile(pcts, 0.75), 2) if pcts else 0.0,
        'win_count': sum(1 for p in pcts if p > 0),
        'loss_count': sum(1 for p in pcts if p < 0),
        'flat_count': sum(1 for p in pcts if p == 0),
        'win_rate': round(sum(1 for p in pcts if p > 0) / len(pcts) * 100, 1) if pcts else 0.0,
    }

    group_rows = {
        name: [r for r in rows if r.get('group') == name]
        for name in ('A', 'B', 'Q', 'F', 'IDX')
    }

    def group_stats(gr):
        # Money totals include every lifecycle/readiness state. Distribution
        # statistics include only active-ready strategies.
        eq = sum(r['equity'] for r in gr)
        init = sum(r['initial_cash'] for r in gr)
        active_ready = [
            r for r in gr
            if (r.get('status') or 'active') != 'retired'
            and (r.get('runtime_status') or 'ready') == 'ready'
        ]
        active_nontradeable = [
            r for r in gr
            if (r.get('status') or 'active') != 'retired'
            and (r.get('runtime_status') or 'ready') != 'ready'
        ]
        retired = [r for r in gr if (r.get('status') or 'active') == 'retired']
        pcts_g = sorted([(r['equity'] - r['initial_cash']) / r['initial_cash'] * 100 for r in active_ready])
        return {
            'count': len(gr),
            # Compatibility alias: historically active_count meant ready.
            'active_count': len(active_ready),
            'active_ready_count': len(active_ready),
            'active_nontradeable_count': len(active_nontradeable),
            'retired_count': len(retired),
            'equity': round(eq, 2),
            'pnl': round(eq - init, 2),
            'avg_pnl': round((eq - init) / max(len(gr), 1), 2),
            'median_pct': round(_quantile(pcts_g, 0.5), 2) if pcts_g else 0.0,
            'win_rate': round(sum(1 for p in pcts_g if p > 0) / len(pcts_g) * 100, 1) if pcts_g else 0.0,
        }

    from datetime import datetime, timezone
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    prev_rows = await fetch_all(
        '''
        SELECT m.account_id AS name, a.equity
        FROM account_meta m
        JOIN accounts a ON a.id = (
            SELECT x.id FROM accounts x
            WHERE x.market=m.market AND x.name=m.account_id AND x.timestamp < :ts
            ORDER BY x.timestamp DESC LIMIT 1
        )
        WHERE m.market=:market
        ''',
        {'ts': today_start, 'market': market},
    )
    prev_equity = {r['name']: r['equity'] for r in prev_rows}
    baseline = sum(prev_equity.get(r['name'], r['initial_cash']) for r in rows)
    daily_pnl = total_equity - baseline

    payload = {
        'market': market,
        'total_equity': round(total_equity, 2),
        'total_pnl': round(total_pnl, 2),
        'total_initial': total_initial,
        'account_count': len(rows),
        'daily_pnl': round(daily_pnl, 2),
        **{f'group_{name}': group_stats(group_rows[name]) for name in group_rows},
        'distribution': distribution,
    }
    return payload


@router.get('/summary')
async def summary(market: str = Query('US')):
    market = _validate_market(market)
    return await _cached_api_value(
        'summary', market, 15, lambda: _build_summary(market), stale_ttl=60,
    )


async def _build_accounts(market: str):
    rows = await fetch_all('''
        SELECT m.account_id AS name, a.cash, a.equity, a.timestamp,
               m."group", m.strategy_name, m.factors, m.status, m.initial_cash,
               m.retired_at, m.retire_reason, m.created_at,
               COALESCE(m.runtime_status,'ready') AS runtime_status,
               m.runtime_reason, m.runtime_detail, m.runtime_updated_at
        FROM account_meta m
        LEFT JOIN accounts a ON a.id = (
            SELECT x.id FROM accounts x
            WHERE x.market=m.market AND x.name=m.account_id
            ORDER BY x.timestamp DESC LIMIT 1
        )
        WHERE m.market=:market
        ORDER BY a.name
    ''', {'market': market})

    if not rows:
        # Fallback when no `accounts` snapshots exist yet for this market.
        meta_rows = await fetch_all(
            'SELECT account_id as name, "group", strategy_name, factors, status, initial_cash, '
            'retired_at, retire_reason, created_at, '
            "COALESCE(runtime_status,'ready') AS runtime_status, runtime_reason, runtime_detail, runtime_updated_at "
            'FROM account_meta WHERE market = :market ORDER BY account_id',
            {'market': market}
        )
        state_rows = await fetch_all(
            'SELECT account, cash, updated_at as timestamp '
            'FROM account_state WHERE market = :market', {'market': market}
        )
        state_by_acc = {r['account']: r for r in state_rows}
        rows = []
        for m in meta_rows:
            st = state_by_acc.get(m['name'], {})
            cash = st.get('cash', m.get('initial_cash') or 10000)
            rows.append({
                'name': m['name'],
                'cash': cash,
                'equity': cash,
                'timestamp': st.get('timestamp'),
                'group': m.get('group') or '',
                'strategy_name': m.get('strategy_name') or '',
                'factors': m.get('factors') or '',
                'status': m.get('status') or 'active',
                'runtime_status': m.get('runtime_status') or 'ready',
                'runtime_reason': m.get('runtime_reason'),
                'runtime_detail': m.get('runtime_detail'),
                'runtime_updated_at': m.get('runtime_updated_at'),
            })

    # LEFT JOIN preserves tombstones/retired experiments that never produced a
    # snapshot. Fill those rows from account_state, then configured initial cash.
    if any(r.get('equity') is None for r in rows):
        state_rows = await fetch_all(
            'SELECT account,cash,initial_cash,updated_at AS timestamp '
            'FROM account_state WHERE market=:market', {'market': market}
        )
        state_by_acc = {r['account']: r for r in state_rows}
        for r in rows:
            if r.get('equity') is not None:
                continue
            state = state_by_acc.get(r['name'], {})
            initial = r.get('initial_cash') or (100000 if market == 'CN' else 10000)
            cash = state.get('cash') if state.get('cash') is not None else initial
            r['cash'] = cash
            r['equity'] = cash
            r['timestamp'] = state.get('timestamp')

    trade_rows = await fetch_all(
        'SELECT account, COUNT(*) as cnt FROM trades WHERE market = :market GROUP BY account',
        {'market': market}
    )
    trade_counts = {r['account']: r['cnt'] for r in trade_rows}
    eq_rows = await _fetch_account_equity_rows(market, since_45d=True)
    eq_by_acc: dict = {}
    for name, equity, _ts, _cash in eq_rows:
        eq_by_acc.setdefault(name, []).append(equity)

    def compute_sharpe(equities):
        if not equities or len(equities) < 3:
            return 0.0
        returns = []
        for i in range(1, len(equities)):
            prev = equities[i - 1]
            if prev and prev > 0:
                returns.append((equities[i] - prev) / prev)
        if len(returns) < 2:
            return 0.0
        n = len(returns)
        mean = sum(returns) / n
        var = sum((x - mean) ** 2 for x in returns) / (n - 1)
        std = var ** 0.5
        if std == 0:
            return 0.0
        import math
        return mean / std * math.sqrt(252 * 6.5)

    result = []
    for r in rows:
        initial = r.get('initial_cash') or (100000 if market == 'CN' else 10000)
        pnl = r['equity'] - initial
        acc_id = r['name']
        sharpe = compute_sharpe(eq_by_acc.get(acc_id, []))
        annualized = _annualized_return_pct(initial, r['equity'], r.get('created_at'), r.get('timestamp'))
        result.append({
            'account_id': acc_id,
            'group': r.get('group', ''),
            'strategy_name': r.get('strategy_name', ''),
            'cash': round(r['cash'], 2),
            'equity': round(r['equity'], 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl / initial * 100, 2),
            'annualized_return_pct': round(annualized, 2) if annualized is not None else None,
            'account_age_days': _account_age_days(r.get('created_at'), r.get('retired_at')),
            'factors': r.get('factors', ''),
            'status': r.get('status', 'active'),
            'runtime_status': r.get('runtime_status', 'ready'),
            'runtime_reason': r.get('runtime_reason'),
            'runtime_detail': r.get('runtime_detail'),
            'runtime_updated_at': r.get('runtime_updated_at'),
            'retired_at': r.get('retired_at'),
            'retire_reason': r.get('retire_reason'),
            'created_at': r.get('created_at'),
            'timestamp': r['timestamp'],
            'trade_count': trade_counts.get(acc_id, 0),
            'sharpe_ratio': round(sharpe, 3),
        })
    return result


@router.get('/accounts')
async def accounts(market: str = Query('US')):
    market = _validate_market(market)
    return await _cached_api_value(
        'accounts', market, 15, lambda: _build_accounts(market), stale_ttl=60,
    )


async def _build_equity_curves(market: str):
    # Pull retired metadata so we can truncate frozen accounts at retired_at
    # (snapshots written after retirement are skipped by update_prices.py, but
    # any historical drift is still visible — clip server-side to be safe).
    meta_rows = await fetch_all(
        "SELECT account_id, \"group\", status, retired_at, retire_reason, initial_cash "
        "FROM account_meta WHERE market = :market",
        {'market': market}
    )
    meta_by_acct = {r['account_id']: dict(r) for r in meta_rows}
    rows = await _fetch_account_equity_rows(market)

    # Dedup overview curves to ≤1 point per hour per account. Upstream writers
    # can emit many snapshots per hour; more importantly, per-account timestamps
    # often differ by milliseconds, so LightweightCharts treats them as distinct
    # logical bars across 40+ series and cannot zoom out to the full Apr→now
    # history. Canonicalize every point to the bucket timestamp so all accounts
    # share the same time axis. Per-account detail charts still use raw snapshots.
    from datetime import datetime as _dt, timezone as _tz
    BUCKET_SEC = 60 * 60

    def _bucket_key(ts: str) -> int:
        try:
            epoch = int(_dt.fromisoformat(ts.replace('Z', '+00:00')).timestamp())
        except Exception:
            return 0
        return epoch - (epoch % BUCKET_SEC)

    def _bucket_ts(bucket: int) -> str:
        return _dt.fromtimestamp(bucket, _tz.utc).isoformat()

    base_initial = 100000.0 if market == 'CN' else 10000.0
    # SQL has already selected the deterministic final complete mark per hour.
    # Keep the read-time fallback/outlier guards below, then coarsen history older
    # than 30 days to daily closes so cold payloads remain bounded.
    bucket_values: dict[str, dict[int, list[float]]] = {}
    last_good_equity: dict[str, float] = {}
    # Parse retired_at once → epoch sec for fast comparison
    retired_cutoff: dict[str, int] = {}
    for acct_id, m in meta_by_acct.items():
        if m.get('status') == 'retired' and m.get('retired_at'):
            try:
                retired_cutoff[acct_id] = int(_dt.fromisoformat(
                    m['retired_at'].replace('Z', '+00:00')
                ).timestamp())
            except Exception:
                pass
    for name, equity, ts, cash in rows:
        # Hard clip retired accounts at retired_at
        if name in retired_cutoff:
            try:
                pt_epoch = int(_dt.fromisoformat(
                    ts.replace('Z', '+00:00')
                ).timestamp())
                if pt_epoch > retired_cutoff[name]:
                    continue
            except Exception:
                pass
        initial = (meta_by_acct.get(name) or {}).get('initial_cash') or base_initial
        equity = float(equity or 0)
        cash = float(cash or 0)
        # Read-time guard for historical avg_cost-fallback pollution: if an
        # account is mostly invested (low cash) but equity snaps back near the
        # starting capital after it previously had a materially different real
        # valuation, skip the point rather than drawing a fake V-dip. This is
        # non-destructive; raw rows remain in SQLite for audit/backfill.
        prev_good = last_good_equity.get(name)
        looks_like_fallback = (
            cash < initial * 0.20
            and initial * 0.98 <= equity <= initial * 1.02
            and prev_good is not None
            and abs(prev_good - initial) > initial * 0.05
        )
        if looks_like_fallback:
            continue
        bk = _bucket_key(ts)
        bucket_values.setdefault(name, {}).setdefault(bk, []).append(equity)
        last_good_equity[name] = equity

    def _drop_isolated_outliers(pts: list[dict]) -> list[dict]:
        """Remove single-point overview spikes from historical bad snapshots.

        This is intentionally conservative: only drops a point when both its
        neighbours agree with each other, while the point itself is far from
        both. Real regime moves that persist into the next point are kept.
        Raw DB rows are unchanged.
        """
        if len(pts) < 3:
            return pts
        kept = [pts[0]]
        for i in range(1, len(pts) - 1):
            a = float(pts[i - 1]['equity'])
            b = float(pts[i]['equity'])
            c = float(pts[i + 1]['equity'])
            base = max(abs(a), abs(c), 1.0)
            neighbours_close = abs(a - c) / base <= 0.06
            point_far = abs(b - a) / max(abs(a), 1.0) >= 0.08 and abs(b - c) / max(abs(c), 1.0) >= 0.08
            if neighbours_close and point_far:
                continue
            kept.append(pts[i])
        kept.append(pts[-1])
        return kept

    def _coarsen_old_history(pts: list[dict]) -> list[dict]:
        cutoff = int(_dt.now(_tz.utc).timestamp()) - 30 * 86400
        old_by_day: dict[str, dict] = {}
        recent = []
        for point in pts:
            try:
                epoch = int(_dt.fromisoformat(point['timestamp'].replace('Z', '+00:00')).timestamp())
            except Exception:
                epoch = cutoff
            if epoch < cutoff:
                old_by_day[point['timestamp'][:10]] = point
            else:
                recent.append(point)
        coarsened = list(old_by_day.values()) + recent
        if pts and (not coarsened or coarsened[0]['timestamp'] != pts[0]['timestamp']):
            coarsened.insert(0, pts[0])
        if pts and coarsened[-1]['timestamp'] != pts[-1]['timestamp']:
            coarsened.append(pts[-1])
        return coarsened

    curves: dict[str, list[dict]] = {}
    for name, by_bucket in bucket_values.items():
        pts = []
        for bk, vals in sorted(by_bucket.items()):
            vals = sorted(vals)
            n = len(vals)
            if n % 2:
                eq = vals[n // 2]
            else:
                eq = (vals[n // 2 - 1] + vals[n // 2]) / 2
            pts.append({'equity': round(eq, 2), 'timestamp': _bucket_ts(bk)})
        curves[name] = _downsample_endpoints(
            _coarsen_old_history(_drop_isolated_outliers(pts)),
            OVERVIEW_EQUITY_MAX_POINTS,
        )

    first_row = await fetch_one(
        'SELECT MIN(timestamp) as ts FROM trades '
        'WHERE market = :market AND account IN '
        '(SELECT account_id FROM account_meta WHERE market = :market)',
        {'market': market}
    )
    anchor_ts = first_row['ts'] if first_row else None
    # Align benchmarks to the already-deduped strategy timestamps.
    align_points = [{'timestamp': ts} for ts in sorted({
        p['timestamp']
        for name, pts in curves.items() if not name.startswith('IDX')
        for p in pts
    })]
    align_ts = [
        p['timestamp'] for p in _downsample_endpoints(
            align_points, OVERVIEW_EQUITY_MAX_POINTS,
        )
    ]
    base_initial = 100000.0 if market == 'CN' else 10000.0
    if anchor_ts:
        for b in benchmarks_for(market):
            curve = await rebased_curve(b['ticker'], anchor_ts, initial=base_initial, align_to=align_ts)
            if curve:
                # rebased_curve also includes raw 5m bars for detail consumers.
                # Overview charts intentionally retain only the shared strategy
                # timeline, preventing thousands of QQQ/SPY points from leaking.
                aligned = {p['timestamp']: p for p in curve if p['timestamp'] in set(align_ts)}
                curves[b['label']] = [aligned[ts] for ts in align_ts if ts in aligned]
    # Build per-account meta (status / retired_at / retire_reason) so the
    # frontend can style retired curves (gray dashed, truncated) and label
    # them in legends/tooltips. Only fields useful to the chart are exposed.
    curves_meta: dict[str, dict] = {}
    for name in curves:
        m = meta_by_acct.get(name) or {}
        curves_meta[name] = {
            'group': m.get('group'),
            'status': m.get('status') or 'active',
            'retired_at': m.get('retired_at'),
            'retire_reason': m.get('retire_reason'),
        }
    payload = {'curves': curves, 'meta': curves_meta}
    return payload


_AGGREGATE_EQUITY_GROUPS = ('A', 'B', 'F', 'Q')


def _aggregate_equity_curves(payload: dict, market: str) -> dict:
    """Reduce full account curves to active, timestamp-aligned group medians."""
    curves = payload.get('curves') or {}
    meta = payload.get('meta') or {}
    benchmark_names = {item['label'] for item in benchmarks_for(market)}
    output_curves = {
        name: curves[name] for name in benchmark_names if name in curves
    }
    output_meta = {
        name: {
            **(meta.get(name) or {}),
            'aggregate': False,
            'benchmark': True,
        }
        for name in output_curves
    }

    for group in _AGGREGATE_EQUITY_GROUPS:
        member_series: dict[str, dict[str, float]] = {}
        for name, points in curves.items():
            item_meta = meta.get(name) or {}
            item_group = item_meta.get('group')
            # The name fallback keeps aggregation compatible with a full payload
            # cached by an older worker during a rolling restart.
            if not item_group:
                clean_name = name[1:] if name.startswith('C') else name
                item_group = clean_name[:1]
            if item_group != group or item_meta.get('status', 'active') == 'retired':
                continue
            series: dict[str, float] = {}
            for point in points or []:
                timestamp = point.get('timestamp') or point.get('time') or point.get('date')
                value = point.get('equity', point.get('value'))
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if timestamp and numeric > 0:
                    series[str(timestamp)] = numeric
            if series:
                member_series[name] = series
        if not member_series:
            continue

        def median_value(values: list[float]) -> float:
            ordered = sorted(values)
            middle = len(ordered) // 2
            return ordered[middle] if len(ordered) % 2 else (
                ordered[middle - 1] + ordered[middle]
            ) / 2

        # Chain-link the median period return instead of taking the median of
        # absolute account levels. A newly created account has no prior-period
        # return, so joining the cohort cannot create a fictitious level jump.
        timeline = sorted({timestamp for series in member_series.values() for timestamp in series})
        previous: dict[str, float] = {}
        composite = None
        aggregate_points = []
        for timestamp in timeline:
            established = list(previous)
            period_returns = []
            for name in established:
                prior = previous[name]
                current = member_series[name].get(timestamp, prior)
                period_returns.append(current / prior - 1.0)
                previous[name] = current
            entrants = [
                series[timestamp] for name, series in member_series.items()
                if name not in previous and timestamp in series
            ]
            if composite is None:
                if not entrants:
                    continue
                composite = median_value(entrants)
            elif period_returns:
                composite *= 1.0 + median_value(period_returns)
            for name, series in member_series.items():
                if name not in previous and timestamp in series:
                    previous[name] = series[timestamp]
            aggregate_points.append({'timestamp': timestamp, 'equity': round(composite, 2)})

        aggregate_name = f'{group} · MEDIAN'
        output_curves[aggregate_name] = _downsample_endpoints(
            aggregate_points, OVERVIEW_EQUITY_MAX_POINTS,
        )
        output_meta[aggregate_name] = {
            'status': 'active',
            'group': group,
            'aggregate': True,
            'aggregation': 'chain_linked_median_return',
            'benchmark': False,
            'member_count': len(member_series),
        }

    return {'curves': output_curves, 'meta': output_meta, 'view': 'aggregate'}


async def _full_equity_curves(market: str):
    return await _cached_api_value(
        'equity_curves:full', market, 60, lambda: _build_equity_curves(market),
        stale_ttl=5 * 60,
    )


@router.get('/equity-curves')
async def equity_curves(market: str = Query('US'), view: str = Query('full')):
    market = _validate_market(market)
    # Direct Python callers written before `view` receive FastAPI's Query object.
    view = view.lower() if isinstance(view, str) else 'full'
    if view not in {'full', 'aggregate'}:
        raise HTTPException(status_code=400, detail="invalid equity view; expected 'full' or 'aggregate'")
    if view == 'full':
        return await _full_equity_curves(market)
    return await _cached_api_value(
        'equity_curves:aggregate', market, 60,
        lambda: _aggregate_equity_curves_from_full(market),
        stale_ttl=5 * 60,
    )


async def _aggregate_equity_curves_from_full(market: str):
    return _aggregate_equity_curves(await _full_equity_curves(market), market)


@router.get('/recent-trades')
async def recent_trades(limit: int = Query(20, ge=1, le=200), market: str = Query('US')):
    market = _validate_market(market)
    rows = await fetch_all(
        'SELECT * FROM trades '
        'WHERE market = :market AND account IN (SELECT account_id FROM account_meta WHERE market = :market) '
        'ORDER BY timestamp DESC, id DESC LIMIT :limit',
        {'market': market, 'limit': limit}
    )
    if market == 'CN':
        names = _cn_ticker_names()
        rows = [dict(r) for r in rows]
        for r in rows:
            n = names.get(r.get('ticker'))
            if n:
                r['ticker_name_cn'] = n.get('cn')
                r['ticker_name_en'] = n.get('en')
    return rows


@router.get('/ticker-names')
async def ticker_names(market: str = Query('CN')):
    """Return {ticker: {cn, en}} mapping for the given market.

    Currently only CN is populated (CSI300 + index from akshare).
    US returns {} (yfinance has no canonical zh names).
    """
    market = _validate_market(market)
    if market == 'CN':
        return _cn_ticker_names()
    return {}


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    """Convert tuple rows in the worker, never on the asyncio event loop."""
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _account_detail_sync(account_id: str, market: str) -> dict | None:
    """Read, window and shape the account's heavy data in one worker thread."""
    con = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    try:
        con.execute('PRAGMA query_only = ON')
        # One explicit read transaction prevents mixed-generation components.
        con.execute('BEGIN')
        params = {'a': account_id, 'm': market}
        meta_rows = _rows(con.execute(
            'SELECT * FROM account_meta WHERE account_id = :a AND market = :m', params
        ))
        if not meta_rows:
            return None
        state_rows = _rows(con.execute(
            'SELECT * FROM account_state WHERE account = :a AND market = :m', params
        ))
        positions = _rows(con.execute(
            'SELECT * FROM positions WHERE account = :a AND market = :m', params
        ))

        # Exact lifetime totals are independent of the bounded initial page.
        stats = con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN lower(side) = 'buy' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN lower(side) = 'sell' THEN 1 ELSE 0 END), MIN(timestamp) "
            'FROM trades WHERE account = :a AND market = :m', params
        ).fetchone()
        trade_total, buy_total, sell_total, anchor_ts = stats
        trade_total = int(trade_total or 0)
        buy_total, sell_total = int(buy_total or 0), int(sell_total or 0)
        trades = _rows(con.execute(
            'SELECT * FROM (SELECT * FROM trades WHERE account = :a AND market = :m '
            'ORDER BY timestamp DESC, id DESC LIMIT :lim) ORDER BY timestamp ASC, id ASC',
            {**params, 'lim': ACCOUNT_TRADES_MAX},
        ))

        equity_bounds = con.execute(
            'SELECT MIN(id),MAX(id),COUNT(*) FROM accounts WHERE name = :a AND market = :m',
            params,
        ).fetchone()
        min_equity_id, max_equity_id, equity_source_points = equity_bounds
        equity_source_points = int(equity_source_points or 0)
        if equity_source_points <= ACCOUNT_EQUITY_MAX_POINTS:
            equity_tuples = con.execute(
                'SELECT equity, timestamp FROM accounts '
                'WHERE name = :a AND market = :m ORDER BY timestamp', params
            ).fetchall()
        else:
            # Snapshot ids are append-ordered. Probe evenly-spaced rowids with
            # indexed correlated seeks: bounded transfer without a 20s window
            # sort/materialization over the complete account history.
            target_ids = sorted({
                round(min_equity_id + i * (max_equity_id - min_equity_id) /
                      (ACCOUNT_EQUITY_MAX_POINTS - 1))
                for i in range(ACCOUNT_EQUITY_MAX_POINTS)
            })
            values = ','.join('(?)' for _ in target_ids)
            equity_tuples = con.execute(
                f'WITH targets(id) AS (VALUES {values}) '
                'SELECT (SELECT equity FROM accounts a WHERE a.id>=t.id '
                ' AND a.name=? AND a.market=? ORDER BY a.id LIMIT 1),'
                ' (SELECT timestamp FROM accounts a WHERE a.id>=t.id '
                ' AND a.name=? AND a.market=? ORDER BY a.id LIMIT 1) '
                'FROM targets t',
                (*target_ids, account_id, market, account_id, market),
            ).fetchall()
            # Interleaved account batches can map adjacent probes to the same
            # snapshot. Deduplicate and restore chart time order.
            equity_tuples = sorted({row[1]: row for row in equity_tuples if row[1]}.values(),
                                   key=lambda row: row[1])
        equity = [{'equity': row[0], 'timestamp': row[1]} for row in equity_tuples]

        # SQL performs timestamp/holding windowing and carries the complete
        # snapshot market value, so cash remains exact even when holdings are cut.
        ph_rows = _rows(con.execute(
            'WITH latest_ts AS ('
            ' SELECT timestamp FROM positions_history WHERE account = :a AND market = :m '
            ' GROUP BY timestamp ORDER BY timestamp DESC LIMIT :snap_lim'
            '), ranked AS ('
            ' SELECT ticker, shares, avg_cost, market_price, market_value, unrealized_pnl, timestamp,'
            ' ROW_NUMBER() OVER (PARTITION BY timestamp ORDER BY market_value DESC, ticker) AS rn,'
            ' SUM(COALESCE(market_value, 0)) OVER (PARTITION BY timestamp) AS total_value'
            ' FROM positions_history WHERE account = :a AND market = :m '
            ' AND timestamp IN (SELECT timestamp FROM latest_ts)'
            ') SELECT ticker, shares, avg_cost, market_price, market_value, unrealized_pnl, '
            'timestamp, total_value FROM ranked WHERE rn <= :holding_lim '
            'ORDER BY timestamp ASC, market_value DESC',
            {**params, 'snap_lim': ACCOUNT_SNAPSHOT_MAX,
             'holding_lim': ACCOUNT_HOLDINGS_PER_SNAPSHOT_MAX},
        ))
        snapshot_ts = sorted({row['timestamp'] for row in ph_rows})
        eq_map = {}
        if snapshot_ts:
            placeholders = ','.join('?' for _ in snapshot_ts)
            eq_map = {
                ts: eq for ts, eq in con.execute(
                    'SELECT timestamp,equity FROM accounts WHERE name=? AND market=? '
                    f'AND timestamp IN ({placeholders})',
                    (account_id, market, *snapshot_ts),
                )
            }
        snap_map: dict[str, dict] = {}
        for row in ph_rows:
            snap = snap_map.setdefault(row['timestamp'], {
                'total_value': row['total_value'], 'holdings': [],
            })
            snap['holdings'].append({
                'ticker': row['ticker'], 'shares': row['shares'],
                'avg_cost': row['avg_cost'], 'price': row['market_price'],
                'value': row['market_value'], 'pnl': row['unrealized_pnl'],
                'pnl_pct': (
                    100.0 * row['unrealized_pnl'] / (row['shares'] * row['avg_cost'])
                    if row['unrealized_pnl'] is not None and row['shares'] and row['avg_cost']
                    else None
                ),
            })
        snapshots = []
        for ts, snap in sorted(snap_map.items()):
            eq_val = eq_map.get(ts)
            snapshots.append({
                'timestamp': ts, 'equity': eq_val,
                'cash': eq_val - snap['total_value'] if eq_val is not None else None,
                'holdings': snap['holdings'],
            })

        # Aggregate all trades by timestamp+side for complete chart markers at a
        # fraction of the payload. Downsample only if the aggregate itself is huge.
        marker_rows = _rows(con.execute(
            'SELECT MIN(id) AS id, timestamp, lower(side) AS side, COUNT(*) AS count, '
            'SUM(shares) AS shares, '
            'SUM(shares * price) / NULLIF(SUM(shares), 0) AS price FROM trades '
            'WHERE account = :a AND market = :m GROUP BY timestamp, lower(side) '
            'ORDER BY timestamp, id', params
        ))
        marker_source_points = len(marker_rows)
        trade_markers = _downsample_endpoints(marker_rows, ACCOUNT_TRADE_MARKERS_MAX)
        return {
            'meta': meta_rows[0], 'state': state_rows[0] if state_rows else None,
            'positions': positions, 'trades': trades, 'equity': equity,
            'equity_source_points': equity_source_points, 'snapshots': snapshots,
            'anchor_ts': anchor_ts, 'trade_total': trade_total,
            'trade_stats': {'total': trade_total, 'buys': buy_total, 'sells': sell_total},
            'trade_markers': trade_markers,
            'trade_marker_source_points': marker_source_points,
        }
    finally:
        con.close()


def _account_trades_page_sync(account_id: str, market: str, limit: int,
                              cursor: tuple[str, int] | None) -> dict | None:
    con = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    try:
        con.execute('PRAGMA query_only = ON')
        params = {'a': account_id, 'm': market, 'lim': limit + 1}
        if not con.execute(
            'SELECT 1 FROM account_meta WHERE account_id = :a AND market = :m', params
        ).fetchone():
            return None
        where = ''
        if cursor:
            params.update({'ts': cursor[0], 'id': cursor[1]})
            where = ' AND (timestamp < :ts OR (timestamp = :ts AND id < :id))'
        trades = _rows(con.execute(
            'SELECT * FROM trades WHERE account = :a AND market = :m' + where +
            ' ORDER BY timestamp DESC, id DESC LIMIT :lim', params
        ))
        has_more = len(trades) > limit
        trades = trades[:limit]
        next_cursor = _trade_cursor(trades[-1]['timestamp'], trades[-1]['id']) if has_more else None
        return {'trades': trades, 'next_cursor': next_cursor}
    finally:
        con.close()


@router.get('/account/{account_id}/trades')
async def account_trades(account_id: str, market: str = Query('US'),
                         limit: int = Query(200, ge=1, le=ACCOUNT_TRADE_PAGE_MAX),
                         cursor: str | None = Query(None)):
    market = _validate_market(market)
    account_id = _validate_account_id(account_id)
    payload = await asyncio.to_thread(
        _account_trades_page_sync, account_id, market, limit, _decode_trade_cursor(cursor)
    )
    if payload is None:
        raise HTTPException(status_code=404, detail='account not found in requested market')
    return {'market': market, 'account_id': account_id, **payload}


async def _build_account_detail(account_id: str, market: str):
    detail = await asyncio.to_thread(_account_detail_sync, account_id, market)
    if detail is None:
        raise HTTPException(status_code=404, detail='account not found in requested market')
    meta, state = detail['meta'], detail['state']
    positions, trades = detail['positions'], detail['trades']
    equity, snapshots = detail['equity'], detail['snapshots']
    anchor_ts = detail['anchor_ts']

    align_ts = [r['timestamp'] for r in equity] if equity else None
    benchmark_align_ts = None
    if align_ts:
        benchmark_align_ts = [
            point['timestamp'] for point in _downsample_endpoints(
                [{'timestamp': timestamp} for timestamp in align_ts],
                ACCOUNT_BENCHMARK_MAX_POINTS,
            )
        ]
    benchmarks = []
    base_initial = 100000.0 if market == 'CN' else 10000.0
    if not account_id.startswith('IDX') and anchor_ts:
        benchmark_defs = benchmarks_for(market)
        try:
            # A stale/missing price cache must not turn the account drawer into
            # a multi-second network cold path. Cached overlays usually return
            # immediately; otherwise the core account response wins after 2s.
            curves = await asyncio.wait_for(asyncio.gather(*[
                rebased_curve(b['ticker'], anchor_ts, initial=base_initial, align_to=benchmark_align_ts)
                for b in benchmark_defs
            ]), timeout=2.0)
        except asyncio.TimeoutError:
            curves = []
        for b, curve in zip(benchmark_defs, curves):
            if curve:
                allowed = set(benchmark_align_ts or [])
                if allowed:
                    curve = [point for point in curve if point.get('timestamp') in allowed]
                else:
                    curve = _downsample_endpoints(curve, ACCOUNT_EQUITY_MAX_POINTS)
                if curve:
                    benchmarks.append({'label': b['label'], 'ticker': b['ticker'], 'curve': curve})

    alpha_info = None
    if equity and anchor_ts:
        strat_start = None
        for r in equity:
            if r['timestamp'] >= anchor_ts:
                strat_start = r['equity']
                break
        strat_start = strat_start or equity[0]['equity']
        strat_final = equity[-1]['equity']
        strat_ret = (strat_final / strat_start - 1) if strat_start else 0
        bench_returns = []
        for b in benchmarks:
            if b['curve']:
                bench_returns.append({
                    'label': b['label'],
                    'ret_pct': round((b['curve'][-1]['equity'] / base_initial - 1) * 100, 2),
                    'alpha_pct': round((strat_ret - (b['curve'][-1]['equity'] / base_initial - 1)) * 100, 2),
                })
        alpha_info = {
            'strategy_ret_pct': round(strat_ret * 100, 2),
            'anchor_ts': anchor_ts,
            'benchmarks': bench_returns,
        }

    return {
        'market': market,
        'account_id': account_id,
        'meta': meta,
        'state': state,
        'positions': positions,
        'trades': trades,
        'trade_total': detail['trade_total'],
        'trades_truncated': detail['trade_total'] > len(trades),
        'trades_next_cursor': (
            _trade_cursor(trades[0]['timestamp'], trades[0]['id'])
            if detail['trade_total'] > len(trades) and trades else None
        ),
        'trade_stats': detail['trade_stats'],
        'trade_markers': detail['trade_markers'],
        'equity_curve': equity,
        'snapshots': snapshots,
        'benchmarks': benchmarks,
        'alpha': alpha_info,
        'limits': {
            'equity_points': ACCOUNT_EQUITY_MAX_POINTS,
            'benchmark_points': ACCOUNT_BENCHMARK_MAX_POINTS,
            'snapshot_timestamps': ACCOUNT_SNAPSHOT_MAX,
            'holdings_per_snapshot': ACCOUNT_HOLDINGS_PER_SNAPSHOT_MAX,
            'trades': ACCOUNT_TRADES_MAX,
            'equity_source_points': detail['equity_source_points'],
            'trade_marker_points': ACCOUNT_TRADE_MARKERS_MAX,
            'trade_marker_source_points': detail['trade_marker_source_points'],
        },
    }


@router.get('/account/{account_id}')
async def account_detail(account_id: str, market: str = Query('US')):
    market = _validate_market(market)
    account_id = _validate_account_id(account_id)
    return await _singleflight_api_value(
        f'account_detail:{account_id}', market,
        lambda: _build_account_detail(account_id, market),
    )
