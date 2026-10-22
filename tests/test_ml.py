from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.forecast.backtest import run_backtest, scoreboard
from app.forecast.baselines import to_day_hour_matrix
from app.forecast.ml import build_features, walk_forward, weather_matrices
from app.ingest.weather import LOCATIONS, parse
from tests.fixtures import OPEN_METEO_PREVIOUS_RUNS
from tests.synthetic import LOCAL_TZ, shape

FIRST, LAST = date(2025, 1, 1), date(2025, 12, 31)


@pytest.fixture(scope="module")
def windy_world():
    """Carbon that mostly depends on wind, like Germany's: a weekly profile can't see it, weather can."""
    rng = np.random.default_rng(42)
    start = pd.Timestamp(FIRST, tz=LOCAL_TZ).tz_convert("UTC")
    end = pd.Timestamp(LAST, tz=LOCAL_TZ).tz_convert("UTC") + pd.Timedelta(days=1)
    ts = pd.date_range(start, end, freq="1h", inclusive="left")

    # Wind: weather systems that last a few days (smooth random walk), 2..16 m/s.
    daily = np.clip(9 + np.cumsum(rng.normal(0, 1.5, len(ts) // 24 + 2)) * 0.6, 2, 16)
    wind = np.interp(np.arange(len(ts)) / 24, np.arange(len(daily)), daily)
    wind = np.clip(wind + rng.normal(0, 0.5, len(ts)), 1, 20)

    local = ts.tz_convert(LOCAL_TZ)
    carbon = 2.5 * shape(local) + 650 - 28 * wind + rng.normal(0, 10, len(ts))
    hourly = pd.DataFrame({"ts": ts, "g_per_kwh": carbon})

    # The weather FORECAST: the true wind plus forecast error.
    weather = pd.concat(
        [
            pd.DataFrame(
                {"ts": ts, "location": name, "wind_100m": wind + rng.normal(0, 1.0, len(ts)), "radiation": 0.0, "temp_2m": 10.0}
            )
            for name in LOCATIONS
        ],
        ignore_index=True,
    )
    return hourly, weather


def test_weather_parsing_drops_null_hours():
    api_vars = {"wind_100m": "wind_speed_100m_previous_day2", "radiation": "shortwave_radiation_previous_day2", "temp_2m": "temperature_2m_previous_day2"}
    df = parse(OPEN_METEO_PREVIOUS_RUNS, "north_sea", api_vars)
    assert len(df) == 2
    assert df["wind_100m"].tolist() == [9.5, 11.0]
    assert str(df["ts"].iloc[0]) == "2026-10-20 00:00:00+00:00"


def test_features_have_one_row_per_hour_and_no_target_leak(windy_world):
    hourly, weather = windy_world
    matrix = to_day_hour_matrix(hourly, "g_per_kwh")
    features = build_features(matrix, "carbon", weather_matrices(weather), date(2025, 6, 1), date(2025, 6, 30))
    assert len(features) == 30 * 24
    assert "y" not in [c for c in features.columns if c.startswith(("lag", "wx", "profile", "recent"))]

    # Carbon is only known through T-2, so lag_recent for T must equal the value on T-2.
    row = features[(features["target_day"] == date(2025, 6, 10)) & (features["hour"] == 12)].iloc[0]
    assert row["lag_recent"] == pytest.approx(matrix.loc[date(2025, 6, 8), 12])


def test_walk_forward_never_trains_on_the_future(windy_world):
    hourly, weather = windy_world
    matrix = to_day_hour_matrix(hourly, "g_per_kwh")
    wx = weather_matrices(weather)
    first, last = date(2025, 10, 1), date(2025, 10, 14)

    features = build_features(matrix, "carbon", wx, FIRST + timedelta(days=35), last)
    before, _ = walk_forward(features, "carbon", first, last)

    tampered = matrix.copy()
    tampered.loc[tampered.index >= date(2025, 10, 5)] = 99999.0  # wreck days after the first week
    features_t = build_features(tampered, "carbon", wx, FIRST + timedelta(days=35), last)
    after, _ = walk_forward(features_t, "carbon", first, last)

    # Forecasts for 1-6 Oct were made before 5 Oct was known, so they must be identical.
    early = lambda p: p[p["target_day"] <= date(2025, 10, 6)]["forecast"].to_numpy()  # noqa: E731
    np.testing.assert_allclose(early(before), early(after))


def test_weather_lets_lgbm_beat_profile_and_fixed_rule(windy_world):
    hourly, weather = windy_world
    matrix = to_day_hour_matrix(hourly, "g_per_kwh")
    first, last = date(2025, 7, 1), date(2025, 12, 31)

    baselines = run_backtest(matrix, "carbon", first, last)
    features = build_features(matrix, "carbon", weather_matrices(weather), FIRST + timedelta(days=35), last)
    ml, importance = walk_forward(features, "carbon", first, last)
    board = scoreboard(pd.concat([baselines, ml], ignore_index=True), 4).set_index("model")

    hindsight = next(m for m in board.index if m.endswith("(best in hindsight)"))
    assert board.loc["lgbm", "mae"] < board.loc["profile_4w", "mae"]
    assert board.loc["lgbm", "regret"] < board.loc["profile_4w", "regret"]
    assert board.loc["lgbm", "regret"] < board.loc[hindsight, "regret"]
    # It found the signal that matters (split across the four wind features).
    wind_share = importance[importance["feature"].str.startswith("wx_wind")]["gain_pct"].sum()
    assert wind_share > 15
