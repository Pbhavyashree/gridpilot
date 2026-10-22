from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.forecast.backtest import best_window_start, run_backtest, scoreboard, to_markdown
from app.forecast.baselines import to_day_hour_matrix
from tests.synthetic import hourly_frame


def test_best_window_start():
    values = np.array([5, 5, 1, 1, 5, 5, 0, 9])
    assert best_window_start(values, 2) == 2  # 1+1=2 beats 5+0=5 and 0+9=9


def _predictions_for_one_day(forecasts: dict[str, list[float]], actual: list[float]) -> pd.DataFrame:
    records = []
    for model, values in forecasts.items():
        for hour in range(24):
            records.append((date(2026, 10, 1), hour, model, values[hour], actual[hour]))
    return pd.DataFrame(records, columns=["target_day", "hour", "model", "forecast", "actual"])


def test_hit_and_regret_hand_example():
    actual = [10.0] * 24
    actual[14:18] = [1.0, 1.0, 1.0, 1.0]  # true best 4-hour window starts at 14
    good = actual.copy()
    bad = [10.0] * 24
    bad[2:6] = [0.0] * 4  # this forecast thinks 02:00 is cheapest

    board = scoreboard(_predictions_for_one_day({"naive_day": bad, "naive_week": good, "profile_4w": good}, actual), 4)
    by_model = board.set_index("model")

    assert by_model.loc["naive_week", "hit_rate"] == 1.0
    assert by_model.loc["naive_week", "regret"] == pytest.approx(0.0)
    assert by_model.loc["naive_day", "hit_rate"] == 0.0
    assert by_model.loc["naive_day", "regret"] == pytest.approx(9.0)  # paid 10 instead of 1
    assert by_model.loc["fixed_00:00", "regret"] == pytest.approx(9.0)


def test_backtest_on_noisy_weekly_data_ranks_sensibly():
    hourly = hourly_frame(date(2026, 6, 1), date(2026, 10, 20), "eur_mwh", noise=5.0)
    matrix = to_day_hour_matrix(hourly, "eur_mwh")
    predictions = run_backtest(matrix, "price", date(2026, 7, 15), date(2026, 10, 20))
    board = scoreboard(predictions, 4).set_index("model")

    # Averaging 4 weeks smooths out the noise, so it should beat a single week.
    assert board.loc["profile_4w", "mae"] < board.loc["naive_week", "mae"]
    # Every forecast should schedule better than blindly starting at midnight.
    for model in ["naive_day", "naive_week", "profile_4w"]:
        assert board.loc[model, "regret"] < board.loc["fixed_00:00", "regret"]


def test_markdown_output():
    actual = [10.0] * 24
    board = scoreboard(_predictions_for_one_day({m: actual for m in ["naive_day", "naive_week", "profile_4w"]}, actual), 4)
    md = to_markdown(board, "price", "EUR/MWh", 4, date(2026, 10, 1), date(2026, 10, 1))
    assert "| Model | MAE (EUR/MWh)" in md
    assert "fixed_00:00" in md
