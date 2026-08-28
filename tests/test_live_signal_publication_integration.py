from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from functools import partial
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.live_auto_executor import LiveAutoExecutor
from core.live_signal_adapter import SignalAdapterError, load_b16_signal_batch
from core.live_signal_publication import PublicationError, publish_b16_signal
from core.live_strategy_control import LiveStrategyStore


class FakeClient:
    settings = SimpleNamespace(
        trading_enabled=False, auto_trading_enabled=False, trade_api_token=None,
    )

    @staticmethod
    def normalize_code(symbol):
        value = str(symbol).upper()
        return value if value.startswith("US.") else "US." + value

    def snapshot(self):
        return {"orders": [], "positions": [], "deals": [], "account": {}}

    def quotes(self, symbols):
        return {self.normalize_code(symbol): {
            "code": self.normalize_code(symbol), "last_price": 10.0,
            "bid_price": 9.9, "ask_price": 10.1,
        } for symbol in symbols}


def _rows(source_date, winner):
    return [
        (winner, source_date, "f1", 2.0, "gp_B16"),
        ("OTHER", source_date, "f1", 1.0, "gp_B16"),
    ]


def _source(tmp_path):
    factors = tmp_path / "factors.json"
    factors.write_text(json.dumps({"B16": [{"name": "f1", "expression": "X0"}]}))
    source = tmp_path / "factors.db"
    with sqlite3.connect(source) as con:
        con.execute("""CREATE TABLE factor_values(
            ticker TEXT NOT NULL, date TEXT NOT NULL, factor_name TEXT NOT NULL,
            value REAL, factor_group TEXT NOT NULL,
            PRIMARY KEY(ticker,date,factor_name,factor_group))""")
        con.executemany("INSERT INTO factor_values VALUES(?,?,?,?,?)", _rows("2026-08-26", "OLD"))
    return source, factors


def _active_store(tmp_path):
    store = LiveStrategyStore(tmp_path / "strategy.db", tmp_path / "archives")
    at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc).isoformat()
    with store.connect() as con:
        con.execute("UPDATE strategy_state SET lifecycle='ACTIVE',freeze_latched=0,"
                    "freeze_reason=NULL,last_sync_at=? WHERE id=1", (at,))
    return store


def test_real_publication_loader_executor_planner_chain_is_pit_at_close(tmp_path):
    source, factors = _source(tmp_path)
    publications = tmp_path / "publications.db"
    publish_b16_signal(
        source, factors, publications,
        published_at=datetime(2026, 8, 26, 21, tzinfo=timezone.utc), publish=True,
    )
    with sqlite3.connect(source) as con:
        con.executemany("INSERT INTO factor_values VALUES(?,?,?,?,?)", _rows("2026-08-27", "NEW"))

    # The publisher itself refuses today's mutable rows until the official close.
    with pytest.raises(PublicationError, match="future|completed"):
        publish_b16_signal(
            source, factors, publications,
            published_at=datetime(2026, 8, 27, 19, 59, tzinfo=timezone.utc), publish=True,
        )
    publish_b16_signal(
        source, factors, publications,
        published_at=datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc), publish=True,
    )

    executor = LiveAutoExecutor(
        cast(Any, FakeClient()), _active_store(tmp_path),
        signal_loader=partial(load_b16_signal_batch, publications, factors),
    )
    premarket = executor.shadow(now=datetime(2026, 8, 27, 12, tzinfo=timezone.utc))
    exact_close = executor.shadow(now=datetime(2026, 8, 27, 20, tzinfo=timezone.utc))

    assert premarket["signal_source_date"] == "2026-08-26"
    assert exact_close["signal_source_date"] == "2026-08-27"
    assert premarket["signal_batch_id"] != exact_close["signal_batch_id"]
    assert premarket["broker_mutation"] is exact_close["broker_mutation"] is False


def test_executor_shadow_blocks_without_immutable_publication(tmp_path):
    _, factors = _source(tmp_path)
    missing = tmp_path / "missing-publications.db"
    executor = LiveAutoExecutor(
        cast(Any, FakeClient()), _active_store(tmp_path),
        signal_loader=partial(load_b16_signal_batch, missing, factors),
    )
    with pytest.raises(SignalAdapterError, match="publication"):
        executor.shadow(now=datetime(2026, 8, 27, 12, tzinfo=timezone.utc))
    assert not missing.exists()
