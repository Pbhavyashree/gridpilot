from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.scheduler.placement import InfeasibleJob, ceil_hour, place

T0 = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)


def curve(prices, carbon=None):
    carbon = carbon if carbon is not None else [300.0] * len(prices)
    return pd.DataFrame(
        {"ts": [T0 + timedelta(hours=i) for i in range(len(prices))], "eur_mwh": prices, "g_per_kwh": carbon}
    )


def test_picks_cheapest_window():
    prices = [100.0] * 12
    prices[6:8] = [10.0, 10.0]
    p = place(curve(prices), 2, T0, T0 + timedelta(hours=12))
    assert p.chosen.start == T0 + timedelta(hours=6)
    assert p.chosen.end == T0 + timedelta(hours=8)
    assert p.chosen.cost_eur == pytest.approx(0.02)  # 2 h * 10 EUR/MWh * 1 kW / 1000
    assert p.asap.start == T0
    assert p.savings["cost_pct"] == pytest.approx(90.0)


def test_window_must_end_by_deadline():
    prices = [100.0] * 12
    prices[10:12] = [1.0, 1.0]  # cheapest, but the job wouldn't finish by 10:00
    p = place(curve(prices), 2, T0, T0 + timedelta(hours=10))
    assert p.chosen.end <= T0 + timedelta(hours=10)


def test_deadline_too_tight():
    with pytest.raises(InfeasibleJob):
        place(curve([50.0] * 12), 4, T0, T0 + timedelta(hours=3))


def test_flat_prices_start_as_early_as_possible():
    p = place(curve([50.0] * 12), 3, T0, T0 + timedelta(hours=12))
    assert p.chosen.start == T0


def test_negative_prices_are_preferred():
    prices = [20.0] * 12
    prices[4:6] = [-15.0, -5.0]
    p = place(curve(prices), 2, T0, T0 + timedelta(hours=12))
    assert p.chosen.start == T0 + timedelta(hours=4)
    assert p.chosen.cost_eur < 0


def test_cost_and_carbon_objectives_can_disagree():
    prices = [50.0, 10.0, 50.0, 50.0]
    carbon = [400.0, 400.0, 400.0, 100.0]
    data = curve(prices, carbon)
    assert place(data, 1, T0, T0 + timedelta(hours=4), "cost").chosen.start == T0 + timedelta(hours=1)
    assert place(data, 1, T0, T0 + timedelta(hours=4), "carbon").chosen.start == T0 + timedelta(hours=3)


def test_blend_weight_moves_the_choice():
    prices = [10.0, 30.0, 50.0]
    carbon = [500.0, 200.0, 100.0]
    data = curve(prices, carbon)
    starts = {
        w: place(data, 1, T0, T0 + timedelta(hours=3), "blend", carbon_weight=w).chosen.start for w in (0.0, 0.5, 1.0)
    }
    assert starts[0.0] == T0
    assert starts[1.0] == T0 + timedelta(hours=2)


def test_windows_with_missing_data_are_skipped():
    prices = [1.0, np.nan, 50.0, 50.0, 50.0]
    p = place(curve(prices), 2, T0, T0 + timedelta(hours=5))
    assert p.chosen.start == T0 + timedelta(hours=2)  # 0-2 and 1-3 both touch the gap


def test_earliest_start_is_rounded_up_to_the_hour():
    assert ceil_hour(T0 + timedelta(minutes=1)) == T0 + timedelta(hours=1)
    assert ceil_hour(T0) == T0
    p = place(curve([1.0] + [50.0] * 5), 1, T0 + timedelta(minutes=20), T0 + timedelta(hours=6))
    assert p.chosen.start == T0 + timedelta(hours=1)  # 00:00 already passed


def test_power_scales_cost():
    p = place(curve([100.0] * 2), 1, T0, T0 + timedelta(hours=2), power_kw=50.0)
    assert p.chosen.cost_eur == pytest.approx(5.0)  # 100 EUR/MWh * 50 kW * 1 h


def test_bad_objective():
    with pytest.raises(ValueError):
        place(curve([1.0] * 3), 1, T0, T0 + timedelta(hours=3), "cheapest")
