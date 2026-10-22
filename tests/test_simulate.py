from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.forecast.simulate import SimJob, simulate, to_markdown


def predictions(days: int, price_day: np.ndarray, carbon_day: np.ndarray, good_model_noise: float = 0.0):
    rng = np.random.default_rng(0)
    rows_p, rows_c = [], []
    for i in range(days):
        day = date(2026, 1, 1) + timedelta(days=i)
        for model, noise in (("perfect_copy", 0.0), ("noisy", good_model_noise)):
            fp = price_day + rng.normal(0, noise, 24)
            fc = carbon_day + rng.normal(0, noise, 24)
            for h in range(24):
                rows_p.append((day, h, model, fp[h], price_day[h]))
                rows_c.append((day, h, model, fc[h], carbon_day[h]))
    cols = ["target_day", "hour", "model", "forecast", "actual"]
    return pd.DataFrame(rows_p, columns=cols), pd.DataFrame(rows_c, columns=cols)


def test_simulation_hand_example():
    price = np.full(24, 100.0)
    price[13:15] = 10.0  # cheap at 13:00-15:00
    carbon = np.full(24, 400.0)
    jobs = [SimJob("refresh", 2, 1.0, "cost")]
    p, c = predictions(10, price, carbon)
    table, n_days = simulate(p, c, jobs)
    t = table.set_index("strategy")

    assert n_days == 10
    assert t.loc["cron 00:00", "cost_eur"] == pytest.approx(10 * 2 * 100 / 1000)  # 2 EUR
    assert t.loc["perfect foresight", "cost_eur"] == pytest.approx(10 * 2 * 10 / 1000)  # 0.2 EUR
    assert t.loc["perfect_copy", "share_of_possible_cost"] == pytest.approx(100.0)
    assert t.loc["best fixed hour (hindsight)", "cost_saved_pct"] == pytest.approx(90.0)  # every day is the same


def test_noisy_forecast_lands_between_cron_and_perfect():
    hours = np.arange(24)
    price = 100 + 40 * np.cos((hours - 19) / 24 * 2 * np.pi)
    carbon = 400 - 150 * np.exp(-((hours - 12) ** 2) / 10)
    p, c = predictions(30, price, carbon, good_model_noise=25.0)
    t = simulate(p, c)[0].set_index("strategy")
    assert t.loc["perfect foresight", "cost_eur"] <= t.loc["noisy", "cost_eur"] <= t.loc["cron 00:00", "cost_eur"]

    md = to_markdown(simulate(p, c)[0], 30)
    assert "| perfect foresight |" in md and "Share of possible saving" in md
