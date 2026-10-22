from datetime import date, timedelta

import numpy as np
import pytest

from app.forecast.baselines import (
    forecast_day,
    known_through,
    local_hour_to_utc,
    profile_4w,
    to_day_hour_matrix,
)
from tests.synthetic import hourly_frame


@pytest.fixture
def matrix():
    return to_day_hour_matrix(hourly_frame(date(2026, 8, 1), date(2026, 10, 20), "eur_mwh"), "eur_mwh")


def test_matrix_has_one_row_per_day_and_24_hours(matrix):
    assert list(matrix.columns) == list(range(24))
    assert matrix.index.min() == date(2026, 8, 1)
    assert matrix.index.max() == date(2026, 10, 20)
    assert not matrix.isna().any().any()


def test_dst_days_are_handled():
    m = to_day_hour_matrix(hourly_frame(date(2026, 3, 28), date(2026, 3, 30), "v"), "v")
    assert np.isnan(m.loc[date(2026, 3, 29), 2])  # spring: 02:00 doesn't exist
    m = to_day_hour_matrix(hourly_frame(date(2026, 10, 24), date(2026, 10, 26), "v"), "v")
    assert not m.loc[date(2026, 10, 25)].isna().any()  # autumn: 02:00 twice, averaged


def test_known_through_rules():
    issue = date(2026, 10, 20)
    assert known_through("price", issue) == issue  # day-ahead prices for today are published
    assert known_through("carbon", issue) == issue - timedelta(days=1)  # today still in progress


@pytest.mark.parametrize("metric", ["price", "carbon"])
@pytest.mark.parametrize("model", ["naive_day", "naive_week", "profile_4w"])
def test_forecast_never_uses_future_data(matrix, metric, model):
    target = date(2026, 10, 15)
    before = forecast_day(matrix, metric, target, model)

    tampered = matrix.copy()
    cutoff = known_through(metric, target - timedelta(days=1))
    tampered.loc[tampered.index > cutoff] = 99999.0  # wreck everything that wasn't known yet

    after = forecast_day(tampered, metric, target, model)
    np.testing.assert_array_equal(before, after)


def test_naive_week_is_perfect_on_weekly_pattern(matrix):
    target = date(2026, 10, 15)
    np.testing.assert_allclose(forecast_day(matrix, "price", target, "naive_week"), matrix.loc[target])


def test_carbon_naive_day_uses_two_days_back(matrix):
    target = date(2026, 10, 15)
    forecast = forecast_day(matrix, "carbon", target, "naive_day")
    np.testing.assert_allclose(forecast, matrix.loc[target - timedelta(days=2)])


def test_profile_needs_two_weeks_of_history(matrix):
    target = date(2026, 10, 15)
    only_one_week = matrix.loc[[target - timedelta(days=7)]]
    assert np.isnan(profile_4w(only_one_week, target)).all()


def test_local_hour_to_utc():
    assert str(local_hour_to_utc(date(2026, 10, 20), 10)) == "2026-10-20 08:00:00+00:00"  # summer time
    assert str(local_hour_to_utc(date(2026, 11, 20), 10)) == "2026-11-20 09:00:00+00:00"  # winter time
    assert local_hour_to_utc(date(2026, 3, 29), 2) is None  # doesn't exist
