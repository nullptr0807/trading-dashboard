import math

import numpy as np
import pandas as pd


def _legacy_ic(comp, close, horizon):
    future_ret = close.shift(-(horizon + 1)) / close.shift(-1) - 1
    ret_long = future_ret.stack().rename("future_return").reset_index()
    ret_long.columns = ["date", "ticker", "future_return"]
    merged = comp[["date", "ticker", "signal"]].merge(
        ret_long, on=["date", "ticker"], how="inner"
    )
    rows = []
    for date, group in merged.groupby("date", sort=True):
        group = group.dropna(subset=["signal", "future_return"])
        if len(group) < 30:
            continue
        ic = group["signal"].rank(pct=True).corr(group["future_return"].rank(pct=True))
        if pd.notna(ic) and math.isfinite(float(ic)):
            rows.append({"date": date, "ic": round(float(ic), 5), "n": len(group)})
    return rows


def test_vectorized_horizons_are_equivalent_to_legacy_rank_ic():
    from api.signal_quality import _score_prepared_signal_quality

    rng = np.random.default_rng(42)
    dates = pd.date_range("2025-01-01", periods=90, freq="D").strftime("%Y-%m-%d")
    tickers = [f"T{i:03d}" for i in range(45)]
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0, 0.01, (len(dates), len(tickers))), axis=0)),
        index=dates,
        columns=tickers,
    )
    signal = pd.DataFrame(rng.normal(size=close.shape), index=dates, columns=tickers)
    # Exercise pairwise missing-value and tie ranking semantics.
    signal.iloc[3:8, :4] = np.nan
    signal.iloc[:, 4:8] = 0.5
    close.iloc[12:15, 8:11] = np.nan
    comp = signal.stack().dropna().rename("signal").reset_index()
    comp.columns = ["date", "ticker", "signal"]
    fv = comp.rename(columns={"signal": "value"})
    prepared = {
        "fv": fv,
        "comp": comp,
        "close": close,
        "signal": signal,
        "factor_names": ["test"],
        "spec": {"group": "B", "factor_group": "gp_B16", "direction": 1},
    }

    for horizon in (1, 5, 10, 20):
        actual = _score_prepared_signal_quality("B16", "US", horizon, 20, prepared)
        expected = _legacy_ic(comp, close, horizon)
        assert [{"date": row["date"], "ic": row["ic"], "n": row["n"]} for row in actual["series"]] == expected
        assert actual["summary"]["n_days"] == len(expected)


def test_many_prepares_once_and_scores_every_horizon(monkeypatch):
    import api.signal_quality as quality

    calls = []
    prepared = {"sentinel": True}

    def prepare(account_id, market, horizon, window, _shared=None):
        calls.append((account_id, market, horizon, window))
        assert _shared["prepare_only"] is True
        _shared["prepared"] = prepared
        return {}

    def score(account_id, market, horizon, window, value):
        assert value is prepared
        return {"horizon": horizon, "supported": True}

    monkeypatch.setattr(quality, "_compute_signal_quality_sync", prepare)
    monkeypatch.setattr(quality, "_score_prepared_signal_quality", score)
    result = quality._compute_signal_quality_many_sync("B16", "US", [1, 5, 10, 20], 20)
    assert calls == [("B16", "US", 1, 20)]
    assert list(result) == [1, 5, 10, 20]
