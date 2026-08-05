from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def test_status_is_market_scoped_and_surfaces_degraded_coverage(tmp_path):
    from api.system_status import _status_sync

    db = tmp_path / 'status.db'
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE account_meta(account_id TEXT, market TEXT, status TEXT, retire_reason TEXT,
          runtime_status TEXT, runtime_reason TEXT);
        CREATE TABLE accounts(name TEXT, market TEXT, timestamp TEXT);
        CREATE TABLE operational_health(component TEXT, market TEXT, status TEXT,
          success_at TEXT, source_timestamp TEXT, details TEXT);
        CREATE TABLE risk_regime(market TEXT PRIMARY KEY, state TEXT,
          last_drawdown REAL, last_check_at TEXT);
        """
    )
    con.executemany(
        'INSERT INTO account_meta VALUES (?,?,?,?,?,?)',
        [('A01', 'US', 'active', None, 'ready', None),
         ('A02', 'US', 'active', None, 'non_tradeable', 'NO_ADMISSIBLE_FACTOR'),
         ('CA01', 'CN', 'active', None, 'ready', None)],
    )
    con.execute("INSERT INTO accounts VALUES ('A01','US','2026-08-04T20:00:00+00:00')")
    con.execute(
        "INSERT INTO operational_health VALUES ('update_prices','US','degraded',?,?,?)",
        ('2026-08-04T20:00:00+00:00', '2026-08-04T19:59:00+00:00', json.dumps({'valid': 4, 'expected': 7})),
    )
    con.executemany(
        'INSERT INTO risk_regime VALUES (?,?,?,?)',
        [('US', 'DISARMED', .04, '2026-08-04'), ('CN', 'ARMED', .08, '2026-08-04')],
    )
    con.commit(); con.close()

    result = _status_sync('US', db)
    assert result['risk']['state'] == 'DISARMED'
    assert result['risk']['drawdown'] == .04
    assert result['quote_health']['details']['valid'] == 4
    assert result['valuation']['active_accounts'] == 2
    assert result['valuation']['complete_accounts'] == 1
    assert result['non_tradeable_accounts'][0]['account_id'] == 'A02'
    assert result['non_tradeable_accounts'][0]['status'] == 'non_tradeable'
    assert result['status'] == 'degraded'


def test_missing_risk_state_fails_closed_even_with_fresh_quotes(tmp_path):
    from api.system_status import _status_sync
    now=datetime.now(timezone.utc).isoformat()
    db=tmp_path/'missing-risk.db'; con=sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE account_meta(account_id TEXT,market TEXT,status TEXT,retire_reason TEXT);"
        "CREATE TABLE accounts(name TEXT,market TEXT,timestamp TEXT);"
        "CREATE TABLE operational_health(component TEXT,market TEXT,status TEXT,success_at TEXT,source_timestamp TEXT,details TEXT);"
    )
    con.execute("INSERT INTO account_meta VALUES('A','US','active',NULL)")
    con.execute("INSERT INTO accounts VALUES('A','US',?)",(now,))
    con.execute("INSERT INTO operational_health VALUES('update_prices','US','ok',?,?, '{}')",(now,now))
    con.commit();con.close()
    result=_status_sync('US',db)
    assert result['risk']['state']=='UNKNOWN'
    assert result['status']=='degraded'


def test_stale_health_timestamps_fail_closed(tmp_path):
    from api.system_status import _status_sync
    db=tmp_path/'stale.db';con=sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE account_meta(account_id TEXT,market TEXT,status TEXT,retire_reason TEXT);"
        "CREATE TABLE accounts(name TEXT,market TEXT,timestamp TEXT);"
        "CREATE TABLE operational_health(component TEXT,market TEXT,status TEXT,success_at TEXT,source_timestamp TEXT,details TEXT);"
        "CREATE TABLE risk_regime(market TEXT,state TEXT,last_drawdown REAL,last_check_at TEXT);"
    )
    con.execute("INSERT INTO account_meta VALUES('A','US','active',NULL)")
    con.execute("INSERT INTO accounts VALUES('A','US','2020-01-01T00:00:00+00:00')")
    con.execute("INSERT INTO operational_health VALUES('update_prices','US','ok','2020-01-01T00:00:00+00:00',NULL,'{}')")
    con.execute("INSERT INTO risk_regime VALUES('US','DISARMED',0,'2020-01-01')")
    con.commit();con.close()
    assert _status_sync('US',db)['status']=='degraded'


def test_stale_risk_timestamp_alone_fails_closed(tmp_path):
    from api.system_status import _status_sync
    now=datetime.now(timezone.utc).isoformat()
    db=tmp_path/'stale-risk.db';con=sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE account_meta(account_id TEXT,market TEXT,status TEXT,retire_reason TEXT);"
        "CREATE TABLE accounts(name TEXT,market TEXT,timestamp TEXT);"
        "CREATE TABLE operational_health(component TEXT,market TEXT,status TEXT,success_at TEXT,source_timestamp TEXT,details TEXT);"
        "CREATE TABLE risk_regime(market TEXT,state TEXT,last_drawdown REAL,last_check_at TEXT);"
    )
    con.execute("INSERT INTO account_meta VALUES('A','US','active',NULL)")
    con.execute("INSERT INTO accounts VALUES('A','US',?)",(now,))
    con.execute("INSERT INTO operational_health VALUES('update_prices','US','ok',?,?, '{}')",(now,now))
    con.execute("INSERT INTO risk_regime VALUES('US','ARMED',0,'2020-01-01T00:00:00+00:00')")
    con.commit();con.close()
    assert _status_sync('US',db)['status']=='degraded'


def test_legacy_singleton_is_never_presented_as_market_state(tmp_path):
    from api.system_status import _status_sync

    db = tmp_path / 'legacy.db'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE risk_regime(id INTEGER, state TEXT, last_drawdown REAL, last_check_at TEXT)')
    con.execute("INSERT INTO risk_regime VALUES (1,'ARMED',.09,'2026-08-04')")
    con.commit(); con.close()

    result = _status_sync('US', db)
    assert result['risk']['state'] == 'LEGACY_MIXED_ARMED'
    assert result['status'] == 'degraded'
