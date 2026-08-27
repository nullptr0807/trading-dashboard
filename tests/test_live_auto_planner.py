from __future__ import annotations

from datetime import datetime, timezone
import inspect
import pytest

from core.live_auto_planner import LivePlanError, plan_live_orders
from core.live_signal_adapter import RankedSignal, SignalBatch

NOW = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)


def batch(symbols=("AAA", "BBB", "CCC", "DDD")):
    ranking = tuple(RankedSignal(symbol, 1 - i / 10) for i, symbol in enumerate(symbols))
    return SignalBatch(
        strategy_id="B16", source_date="2026-08-26", factor_names=("f1",),
        factor_set_hash="factors", batch_id="batch", ranking=ranking,
        buy_candidates=symbols[:-1], sell_tail=(symbols[-1],),
    )


def config(**patch):
    result = {
        "strategy_id": "B16", "top_n": 2, "position_target_pct": 0.20,
        "gross_target_pct": 0.50, "stop_loss_pct": 0.08,
        "stop_cooldown_hours": 72, "min_hold_days": 2,
        "hold_band_mult": 2,
    }
    result.update(patch)
    return {"version": 7, "values": result}


def state(**patch):
    result = {"strategy_id": "B16", "lifecycle": "ACTIVE", "config_version": 7,
              "allocated_cash": 1000.0, "strategy_equity": 1000.0,
              "owned_market_value": 0.0, "reserved_buy_notional": 0.0}
    result.update(patch)
    return result


def quotes(**prices):
    return {symbol: {"last_price": price, "bid_price": price - .1,
                     "ask_price": price + .1} for symbol, price in prices.items()}


def test_planner_is_pure_deterministic_and_has_no_paper_state_input():
    assert "paper" not in inspect.signature(plan_live_orders).parameters
    live_state = state(paper_cash=9_000_000, paper_positions={"ZZZ": 999})
    inputs = dict(signal_batch=batch(), live_config=config(), live_state=live_state,
                  owned_positions={}, quotes=quotes(AAA=10, BBB=20, CCC=30),
                  pending_strategy_orders=(), opened_at={}, cooldown_until={}, now=NOW)

    first = plan_live_orders(**inputs)
    second = plan_live_orders(**inputs)
    without_paper_noise = plan_live_orders(**{**inputs, "live_state": state()})

    assert first == second == without_paper_noise
    assert [i.symbol for i in first.intents] == ["AAA", "BBB"]
    assert all(i.side == "BUY" and i.quantity == int(i.quantity) for i in first.intents)
    assert all(i.order_type == "LIMIT" and i.session == "RTH" and i.time_in_force == "DAY"
               for i in first.intents)


def test_live_config_changes_top_n_sizing_and_plan_id():
    common = dict(signal_batch=batch(), live_state=state(), owned_positions={},
                  quotes=quotes(AAA=10, BBB=20, CCC=30), pending_strategy_orders=(),
                  opened_at={}, cooldown_until={}, now=NOW)
    small = plan_live_orders(live_config=config(top_n=1, position_target_pct=.10,
                                                gross_target_pct=.10), **common)
    larger = plan_live_orders(live_config={"version": 8, "values": config(
        top_n=2, position_target_pct=.20, gross_target_pct=.40)["values"]},
        **{**common, "live_state": state(config_version=8)})

    assert [(i.symbol, i.quantity) for i in small.intents] == [("AAA", 9)]
    assert [(i.symbol, i.quantity) for i in larger.intents] == [("AAA", 19), ("BBB", 9)]
    assert small.plan_id != larger.plan_id


def test_sells_have_priority_and_use_live_stop_and_hold_band_rules():
    positions = {
        "AAA": {"quantity": 5, "average_cost": 10},       # in hold band
        "DDD": {"quantity": 7, "average_cost": 10},       # outside hold band, old
        "EEE": {"quantity": 4, "average_cost": 10},       # outside but too new
        "STOP": {"quantity": 3, "average_cost": 10},      # stop overrides min hold
    }
    opened = {"AAA": "2026-08-01T00:00:00+00:00", "DDD": "2026-08-01T00:00:00+00:00",
              "EEE": "2026-08-27T00:00:00+00:00", "STOP": "2026-08-27T00:00:00+00:00"}
    plan = plan_live_orders(
        batch(), config(hold_band_mult=1), state(owned_market_value=190), positions,
        quotes(AAA=10, BBB=20, CCC=30, DDD=10, EEE=10, STOP=8),
        pending_strategy_orders=(), opened_at=opened, cooldown_until={}, now=NOW,
    )

    assert [(i.side, i.symbol, i.quantity, i.purpose) for i in plan.intents[:2]] == [
        ("SELL", "STOP", 3, "STOP_LOSS"),
        ("SELL", "DDD", 7, "RANK_EXIT"),
    ]
    assert all(i.side == "SELL" for i in plan.intents[:2])
    assert not any(i.symbol == "EEE" for i in plan.intents)


def test_pending_strategy_sell_is_subtracted_and_personal_quantity_cannot_enter():
    # Broker/account quantity is 12, but the planner receives and may use only the
    # two shares proven by the live strategy ledger.
    live_state = state(allocated_cash=0, owned_market_value=20,
                       broker_positions={"DDD": {"quantity": 12}})
    plan = plan_live_orders(
        batch(), config(), live_state,
        {"DDD": {"quantity": 2, "average_cost": 10}}, quotes(DDD=9),
        pending_strategy_orders=[{"symbol": "DDD", "side": "SELL", "quantity": 1,
                                  "filled_quantity": 0}],
        opened_at={"DDD": "2026-08-01T00:00:00+00:00"}, cooldown_until={}, now=NOW,
    )
    sells = [i for i in plan.intents if i.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].quantity == 1


def test_pending_buys_cash_gross_and_cooldown_bound_new_orders():
    plan = plan_live_orders(
        batch(), config(top_n=2, position_target_pct=.20, gross_target_pct=.50),
        state(allocated_cash=350, owned_market_value=100), {},
        quotes(AAA=10, BBB=20, CCC=25),
        pending_strategy_orders=[{"symbol": "AAA", "side": "BUY", "quantity": 10,
                                  "filled_quantity": 0, "limit_price": 10}],
        opened_at={}, cooldown_until={"BBB": "2026-08-28T00:00:00+00:00"}, now=NOW,
    )
    # Pending AAA reserves $100 and counts toward its target. BBB is cooling down,
    # so the deterministic fallback is CCC. Cash and gross caps remain live-only.
    assert [(i.symbol, i.quantity) for i in plan.intents] == [("AAA", 9), ("CCC", 6)]


def test_stop_loss_sell_cannot_rebuy_same_symbol_in_same_plan():
    plan = plan_live_orders(
        batch(("STOP", "AAA", "BBB", "CCC")), config(top_n=1), state(),
        {"STOP": {"quantity": 2, "average_cost": 10}},
        quotes(STOP=8, AAA=10),
        pending_strategy_orders=(), opened_at={"STOP": "2026-08-27T00:00:00+00:00"},
        cooldown_until={}, now=NOW,
    )
    assert [(intent.side, intent.symbol) for intent in plan.intents] == [
        ("SELL", "STOP"), ("BUY", "AAA")
    ]


def test_planner_does_not_mutate_any_input():
    owned = {"DDD": {"quantity": 2, "average_cost": 10}}
    pending = [{"symbol": "DDD", "side": "SELL", "quantity": 1, "filled_quantity": 0}]
    before_owned = {k: dict(v) for k, v in owned.items()}
    before_pending = [dict(v) for v in pending]
    plan_live_orders(batch(), config(), state(allocated_cash=0), owned, quotes(DDD=10),
                     pending_strategy_orders=pending,
                     opened_at={"DDD": "2026-08-01T00:00:00+00:00"},
                     cooldown_until={}, now=NOW)
    assert owned == before_owned
    assert pending == before_pending


def test_planner_fails_closed_when_live_inputs_are_missing_or_versions_differ():
    inputs = dict(
        signal_batch=batch(), live_config=config(), live_state=state(), owned_positions={},
        quotes=quotes(AAA=10, BBB=20, CCC=30), pending_strategy_orders=(),
        opened_at={}, cooldown_until={}, now=NOW,
    )
    for key in ("pending_strategy_orders", "opened_at", "cooldown_until", "now"):
        incomplete = dict(inputs)
        incomplete.pop(key)
        with pytest.raises(TypeError):
            plan_live_orders(**incomplete)
    for key in ("owned_market_value", "reserved_buy_notional", "config_version", "lifecycle"):
        incomplete_state = state()
        incomplete_state.pop(key)
        with pytest.raises(LivePlanError):
            plan_live_orders(**{**inputs, "live_state": incomplete_state})
    with pytest.raises(LivePlanError, match="versions do not match"):
        plan_live_orders(**{**inputs, "live_state": state(config_version=99)})
