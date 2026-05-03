from fastapi import APIRouter, Query, HTTPException
from core.db import fetch_all, fetch_one
from core.benchmarks import rebased_curve, benchmarks_for
import os, json
from functools import lru_cache

router = APIRouter(prefix='/api/trade', tags=['trade'])

VALID_MARKETS = {'US', 'CN'}

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


@router.get('/summary')
async def summary(market: str = Query('US')):
    market = _validate_market(market)
    # Source of truth for market = account_meta.market (the `accounts` snapshot
    # table inherits the default 'US' for everything, so we always join through
    # account_meta to filter properly).
    rows = await fetch_all('''
        SELECT a.name, a.cash, a.equity, a.timestamp,
               m."group", m.strategy_name, m.initial_cash, m.status
        FROM accounts a
        JOIN (SELECT name, MAX(timestamp) as max_ts FROM accounts GROUP BY name) latest
            ON a.name = latest.name AND a.timestamp = latest.max_ts
        JOIN account_meta m ON a.name = m.account_id
        WHERE m.market = :market
    ''', {'market': market})

    # If `accounts` is empty for this market (e.g. CN before first cron tick),
    # fall back to account_state + account_meta so the dashboard still shows
    # all configured accounts at their initial equity.
    if not rows:
        meta_rows = await fetch_all(
            'SELECT account_id as name, "group", strategy_name, initial_cash, status '
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
            })

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
            'pnl': round(r['equity'] - r['initial_cash'], 2),
            'pnl_pct': round((r['equity'] - r['initial_cash']) / r['initial_cash'] * 100, 2),
        }
        for r in rows
    ]
    # Retired accounts are frozen — their final return is real (we keep it in
    # totals + per_account for display) but excluding them from distribution
    # stats avoids skewing median/IQR/win-rate/best/worst with stale figures.
    active_for_dist = [a for a in per_account if a.get('status') != 'retired']
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
        'accounts': per_account,
    }

    a_rows = [r for r in rows if r.get('group') == 'A']
    b_rows = [r for r in rows if r.get('group') == 'B']
    q_rows = [r for r in rows if r.get('group') == 'Q']

    def group_stats(gr):
        # totals include retired accounts (real money), but distribution stats
        # (median / win_rate) are computed only over active to avoid drift.
        eq = sum(r['equity'] for r in gr)
        init = sum(r['initial_cash'] for r in gr)
        active = [r for r in gr if (r.get('status') or 'active') != 'retired']
        pcts_g = sorted([(r['equity'] - r['initial_cash']) / r['initial_cash'] * 100 for r in active])
        return {
            'count': len(gr),
            'active_count': len(active),
            'retired_count': len(gr) - len(active),
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
        SELECT a.name, a.equity
        FROM accounts a
        JOIN (
            SELECT name, MAX(timestamp) AS max_ts
            FROM accounts
            WHERE timestamp < :ts
            GROUP BY name
        ) prev ON a.name = prev.name AND a.timestamp = prev.max_ts
        WHERE a.name IN (SELECT account_id FROM account_meta WHERE market = :market)
        ''',
        {'ts': today_start, 'market': market},
    )
    prev_equity = {r['name']: r['equity'] for r in prev_rows}
    baseline = sum(prev_equity.get(r['name'], r['initial_cash']) for r in rows)
    daily_pnl = total_equity - baseline

    return {
        'market': market,
        'total_equity': round(total_equity, 2),
        'total_pnl': round(total_pnl, 2),
        'total_initial': total_initial,
        'account_count': len(rows),
        'daily_pnl': round(daily_pnl, 2),
        'group_A': group_stats(a_rows),
        'group_B': group_stats(b_rows),
        'group_Q': group_stats(q_rows),
        'distribution': distribution,
    }


@router.get('/accounts')
async def accounts(market: str = Query('US')):
    market = _validate_market(market)
    rows = await fetch_all('''
        SELECT a.name, a.cash, a.equity, a.timestamp,
               m."group", m.strategy_name, m.factors, m.status, m.initial_cash,
               m.retired_at, m.retire_reason, m.created_at
        FROM accounts a
        JOIN (SELECT name, MAX(timestamp) as max_ts FROM accounts GROUP BY name) latest
            ON a.name = latest.name AND a.timestamp = latest.max_ts
        JOIN account_meta m ON a.name = m.account_id
        WHERE m.market = :market
        ORDER BY a.name
    ''', {'market': market})

    if not rows:
        # Fallback when no `accounts` snapshots exist yet for this market.
        meta_rows = await fetch_all(
            'SELECT account_id as name, "group", strategy_name, factors, status, initial_cash, '
            'retired_at, retire_reason, created_at '
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
            })

    trade_rows = await fetch_all(
        'SELECT account, COUNT(*) as cnt FROM trades WHERE market = :market GROUP BY account',
        {'market': market}
    )
    trade_counts = {r['account']: r['cnt'] for r in trade_rows}
    eq_rows = await fetch_all(
        'SELECT name, equity, timestamp FROM accounts WHERE market = :market ORDER BY name, timestamp',
        {'market': market}
    )
    eq_by_acc: dict = {}
    for r in eq_rows:
        eq_by_acc.setdefault(r['name'], []).append(r['equity'])

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
        result.append({
            'account_id': acc_id,
            'group': r.get('group', ''),
            'strategy_name': r.get('strategy_name', ''),
            'cash': round(r['cash'], 2),
            'equity': round(r['equity'], 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl / initial * 100, 2),
            'factors': r.get('factors', ''),
            'status': r.get('status', 'active'),
            'retired_at': r.get('retired_at'),
            'retire_reason': r.get('retire_reason'),
            'created_at': r.get('created_at'),
            'timestamp': r['timestamp'],
            'trade_count': trade_counts.get(acc_id, 0),
            'sharpe_ratio': round(sharpe, 3),
        })
    return result


@router.get('/equity-curves')
async def equity_curves(market: str = Query('US')):
    market = _validate_market(market)
    # Pull retired metadata so we can truncate frozen accounts at retired_at
    # (snapshots written after retirement are skipped by update_prices.py, but
    # any historical drift is still visible — clip server-side to be safe).
    meta_rows = await fetch_all(
        "SELECT account_id, status, retired_at, retire_reason "
        "FROM account_meta WHERE market = :market",
        {'market': market}
    )
    meta_by_acct = {r['account_id']: dict(r) for r in meta_rows}
    rows = await fetch_all(
        'SELECT name, equity, timestamp FROM accounts '
        'WHERE name IN (SELECT account_id FROM account_meta WHERE market = :market) '
        'ORDER BY name, timestamp',
        {'market': market}
    )

    # Dedup to ≤1 point per 15-min bucket per account. Upstream update_prices.py
    # can write 60 near-identical equity rows/hour while a market is closed;
    # that visually squashes earlier trading days on the chart. Keep the LAST
    # value seen within each bucket so the most recent equity wins.
    from datetime import datetime as _dt
    BUCKET_SEC = 15 * 60

    def _bucket_key(ts: str) -> int:
        try:
            epoch = int(_dt.fromisoformat(ts.replace('Z', '+00:00')).timestamp())
        except Exception:
            return 0
        return epoch - (epoch % BUCKET_SEC)

    curves: dict[str, list[dict]] = {}
    last_bucket: dict[str, int] = {}  # name → last bucket_key
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
    for r in rows:
        name = r['name']
        # Hard clip retired accounts at retired_at
        if name in retired_cutoff:
            try:
                pt_epoch = int(_dt.fromisoformat(
                    r['timestamp'].replace('Z', '+00:00')
                ).timestamp())
                if pt_epoch > retired_cutoff[name]:
                    continue
            except Exception:
                pass
        bk = _bucket_key(r['timestamp'])
        buf = curves.setdefault(name, [])
        if last_bucket.get(name) == bk and buf:
            # Replace — keep the last value within this 15-min bucket
            buf[-1] = {'equity': round(r['equity'], 2), 'timestamp': r['timestamp']}
        else:
            buf.append({'equity': round(r['equity'], 2), 'timestamp': r['timestamp']})
            last_bucket[name] = bk

    first_row = await fetch_one(
        'SELECT MIN(timestamp) as ts FROM trades '
        'WHERE account IN (SELECT account_id FROM account_meta WHERE market = :market)',
        {'market': market}
    )
    anchor_ts = first_row['ts'] if first_row else None
    # Align benchmarks to the already-deduped strategy timestamps.
    align_ts = sorted({
        p['timestamp']
        for name, pts in curves.items() if not name.startswith('IDX')
        for p in pts
    })
    base_initial = 100000.0 if market == 'CN' else 10000.0
    if anchor_ts:
        for b in benchmarks_for(market):
            curve = await rebased_curve(b['ticker'], anchor_ts, initial=base_initial, align_to=align_ts)
            if curve:
                curves[b['label']] = curve
    # Build per-account meta (status / retired_at / retire_reason) so the
    # frontend can style retired curves (gray dashed, truncated) and label
    # them in legends/tooltips. Only fields useful to the chart are exposed.
    curves_meta: dict[str, dict] = {}
    for name in curves:
        m = meta_by_acct.get(name) or {}
        curves_meta[name] = {
            'status': m.get('status') or 'active',
            'retired_at': m.get('retired_at'),
            'retire_reason': m.get('retire_reason'),
        }
    return {'curves': curves, 'meta': curves_meta}


@router.get('/recent-trades')
async def recent_trades(limit: int = Query(20, ge=1, le=200), market: str = Query('US')):
    market = _validate_market(market)
    rows = await fetch_all(
        'SELECT * FROM trades '
        'WHERE account IN (SELECT account_id FROM account_meta WHERE market = :market) '
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


@router.get('/account/{account_id}')
async def account_detail(account_id: str, market: str = Query('US')):
    market = _validate_market(market)
    # Per-account names are globally unique (A01 vs CA01, IDX1 vs IDX3),
    # so we filter only by account_id on the row-level tables — but we
    # validate the account belongs to the requested market via account_meta.
    meta = await fetch_one(
        'SELECT * FROM account_meta WHERE account_id = :a AND market = :m',
        {'a': account_id, 'm': market}
    )
    state = await fetch_one(
        'SELECT * FROM account_state WHERE account = :a',
        {'a': account_id}
    )
    positions = await fetch_all(
        'SELECT * FROM positions WHERE account = :a',
        {'a': account_id}
    )
    trades = await fetch_all(
        'SELECT * FROM trades WHERE account = :a ORDER BY timestamp ASC',
        {'a': account_id}
    )
    equity = await fetch_all(
        'SELECT equity, timestamp FROM accounts WHERE name = :a ORDER BY timestamp',
        {'a': account_id}
    )

    ph_rows = await fetch_all(
        'SELECT ticker, shares, avg_cost, market_price, market_value, unrealized_pnl, timestamp '
        'FROM positions_history WHERE account = :a ORDER BY timestamp ASC',
        {'a': account_id}
    )
    snap_map = {}
    for r in ph_rows:
        ts = r['timestamp']
        snap_map.setdefault(ts, []).append({
            'ticker': r['ticker'],
            'shares': r['shares'],
            'avg_cost': r['avg_cost'],
            'price': r['market_price'],
            'value': r['market_value'],
            'pnl': r['unrealized_pnl'],
            'pnl_pct': (100.0 * r['unrealized_pnl'] / (r['shares'] * r['avg_cost'])) if (r['shares'] and r['avg_cost']) else 0.0,
        })
    eq_map = {e['timestamp']: e['equity'] for e in equity}
    snapshots = []
    for ts in sorted(snap_map.keys()):
        holdings = sorted(snap_map[ts], key=lambda h: (h['value'] or 0), reverse=True)
        total_val = sum(h['value'] or 0 for h in holdings)
        eq_val = eq_map.get(ts)
        snapshots.append({
            'timestamp': ts,
            'equity': eq_val,
            'cash': (eq_val - total_val) if eq_val is not None else None,
            'holdings': holdings,
        })

    first_trade_row = await fetch_one(
        'SELECT MIN(timestamp) as ts FROM trades WHERE account = :a',
        {'a': account_id}
    )
    anchor_ts = first_trade_row['ts'] if first_trade_row else None

    align_ts = [r['timestamp'] for r in equity] if equity else None
    benchmarks = []
    base_initial = 100000.0 if market == 'CN' else 10000.0
    if not account_id.startswith('IDX') and anchor_ts:
        for b in benchmarks_for(market):
            curve = await rebased_curve(b['ticker'], anchor_ts, initial=base_initial, align_to=align_ts)
            if curve:
                benchmarks.append({
                    'label': b['label'],
                    'ticker': b['ticker'],
                    'curve': curve,
                })

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
        'equity_curve': equity,
        'snapshots': snapshots,
        'benchmarks': benchmarks,
        'alpha': alpha_info,
    }
