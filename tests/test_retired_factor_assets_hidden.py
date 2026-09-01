import asyncio


def test_retired_factor_endpoint_does_not_rebuild_shared_model_details(monkeypatch):
    import api.factors as factors

    async def fake_fetch_one(query, params):
        return {
            'account_id': 'Q02', 'market': 'US', 'group': 'Q',
            'status': 'retired', 'strategy_name': 'must-not-leak',
            'factors': 'qlib_Q02_score (XGBoost)',
        }

    monkeypatch.setattr(factors, 'fetch_one', fake_fetch_one)
    result = asyncio.run(factors.get_factors('Q02', market='US'))
    assert result == {
        'account_id': 'Q02', 'group': 'Q', 'status': 'retired',
        'factors': [], 'gp_info': '', 'gp_params': [], 'composite': {},
        'note': 'retired_account_model_assets_removed',
    }