from datetime import datetime, timedelta, timezone

import pytest

from api.trade import _account_age_days, _annualized_return_pct


def test_annualized_return_uses_inception_to_latest_mark_cagr():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=365.25)
    assert _annualized_return_pct(10_000, 11_000, start.isoformat(), end.isoformat()) == pytest.approx(10.0)


@pytest.mark.parametrize(
    "initial,equity,created_at,as_of",
    [
        (0, 11_000, "2025-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        (10_000, 11_000, None, "2026-01-01T00:00:00+00:00"),
        (10_000, 11_000, "2026-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
    ],
)
def test_annualized_return_is_unavailable_for_invalid_inputs(initial, equity, created_at, as_of):
    assert _annualized_return_pct(initial, equity, created_at, as_of) is None


def test_account_age_uses_now_for_active_and_retirement_for_retired():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = start + timedelta(days=10, hours=23)
    retired = start + timedelta(days=4, hours=12)
    assert _account_age_days(start, now=now) == 10
    assert _account_age_days(start, retired_at=retired, now=now) == 4