"""LightGBM forecaster with weather features, evaluated walk-forward.

Features for target day T (forecast made at 10:00 on T-1), one row per hour:

  calendar  hour, weekday, month, weekend, German public holiday, season
  history   same hour 7 days ago, same hour on the latest known day, 4-week profile,
            mean / min / max of the latest known day
  weather   wind at 100 m (all of Germany and the windy north), solar radiation
            (all and the sunny south), temperature, plus their daily means for T.
            These are FORECASTS made ~2 days earlier (see app/ingest/weather.py).

Every feature respects the same "what was known when" rule as the baselines
(known_through in baselines.py), so the model can't peek at the future.

Walk-forward training: retrain every 28 days on everything known so far (an expanding
window), then forecast the next 28 days with that model. This mirrors how it would
run live: retrained monthly, never trained on the days it is scored on.
"""

import logging
import warnings
from datetime import date, timedelta

import holidays
import numpy as np
import pandas as pd

from app.forecast.baselines import HOURS, known_through, to_day_hour_matrix

log = logging.getLogger(__name__)

MODEL_NAME = "lgbm"
RETRAIN_EVERY_DAYS = 28
MIN_TRAIN_DAYS = 120

NUM_BOOST_ROUND = 400
LGBM_PARAMS = {
    "objective": "regression_l1",  # robust to price spikes; we care about the day's shape, not outliers
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 30,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "feature_fraction": 0.8,
    "seed": 0,
    "deterministic": True,
    "num_threads": 4,
    "verbose": -1,
}

WEATHER_GROUPS = {
    "wind_all": ("wind_100m", None),
    "wind_north": ("wind_100m", ["north_sea", "baltic", "brandenburg"]),
    "radiation_all": ("radiation", None),
    "radiation_south": ("radiation", ["bavaria", "southwest"]),
    "temp_all": ("temp_2m", None),
}


# ---------- weather ----------


def weather_matrices(weather: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Long weather table (ts, location, wind_100m, radiation, temp_2m) -> one day x hour matrix per feature."""
    if weather.empty:
        return {}
    weather = weather.copy()
    weather["ts"] = pd.to_datetime(weather["ts"], utc=True)
    out = {}
    for name, (column, locations) in WEATHER_GROUPS.items():
        subset = weather if locations is None else weather[weather["location"].isin(locations)]
        hourly = subset.groupby("ts")[column].mean().dropna().reset_index()
        out[name] = to_day_hour_matrix(hourly, column)
    return out


# ---------- features ----------


def _full_days(matrix: pd.DataFrame, first: date, last: date) -> pd.DataFrame:
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return matrix.reindex(index=days, columns=HOURS)


def _long(matrix: pd.DataFrame, name: str) -> pd.Series:
    series = matrix.stack(future_stack=True)
    series.index.names = ["target_day", "hour"]
    return series.rename(name)


def _broadcast(day_values: pd.Series, like: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(np.repeat(day_values.to_numpy()[:, None], 24, axis=1), index=like.index, columns=HOURS)


def build_features(
    target: pd.DataFrame, metric: str, weather: dict[str, pd.DataFrame], first: date, last: date
) -> pd.DataFrame:
    """One row per (target_day, hour) in [first, last]; column 'y' is the actual value (NaN if unknown)."""
    # The latest complete day known at 10:00 on T-1: T-1 for price, T-2 for carbon.
    issue = first - timedelta(days=1)
    lag_recent_days = (first - known_through(metric, issue)).days
    history_start = first - timedelta(days=35)
    full = _full_days(target, history_start, last)

    recent = full.shift(lag_recent_days)
    lags = {k: full.shift(7 * k) for k in range(1, 5)}
    stacked = np.stack([m.to_numpy() for m in lags.values()])
    counts = np.sum(~np.isnan(stacked), axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # all-NaN hours
        profile = np.where(counts >= 2, np.nanmean(stacked, axis=0), np.nan)
    profile = pd.DataFrame(profile, index=full.index, columns=HOURS)

    matrices = {
        "y": full,
        "lag_7d": lags[1],
        "lag_recent": recent,
        "profile_4w": profile,
        "recent_day_mean": _broadcast(recent.mean(axis=1), full),
        "recent_day_min": _broadcast(recent.min(axis=1), full),
        "recent_day_max": _broadcast(recent.max(axis=1), full),
    }
    for name, wx in weather.items():
        wx_full = _full_days(wx, history_start, last)
        matrices[f"wx_{name}"] = wx_full
        matrices[f"wx_{name}_daymean"] = _broadcast(wx_full.mean(axis=1), full)

    frame = pd.concat([_long(m, name) for name, m in matrices.items()], axis=1).reset_index()
    frame = frame[(frame["target_day"] >= first) & (frame["target_day"] <= last)]

    days = pd.to_datetime(frame["target_day"])
    de_holidays = holidays.country_holidays("DE", years=range(first.year, last.year + 1))
    frame["hour_of_day"] = frame["hour"]
    frame["weekday"] = days.dt.day_of_week
    frame["month"] = days.dt.month
    frame["is_weekend"] = (frame["weekday"] >= 5).astype(int)
    frame["is_holiday"] = frame["target_day"].map(lambda d: int(d in de_holidays))
    doy = days.dt.day_of_year
    frame["season_sin"] = np.sin(2 * np.pi * doy / 365.25)
    frame["season_cos"] = np.cos(2 * np.pi * doy / 365.25)
    return frame.reset_index(drop=True)


def feature_columns(frame: pd.DataFrame) -> list[str]:
    return [c for c in frame.columns if c not in ("target_day", "hour", "y")]


# ---------- walk-forward ----------


def walk_forward(
    features: pd.DataFrame,
    metric: str,
    first_target: date,
    last_target: date,
    retrain_every: int = RETRAIN_EVERY_DAYS,
    min_train_days: int = MIN_TRAIN_DAYS,
    params: dict | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (predictions in the backtest's long format, feature importance)."""
    import lightgbm as lgb

    params = {**LGBM_PARAMS, **(params or {})}
    cols = feature_columns(features)
    predictions, importances = [], []

    block_start = first_target
    while block_start <= last_target:
        block_end = min(block_start + timedelta(days=retrain_every - 1), last_target)
        cutoff = known_through(metric, block_start - timedelta(days=1))
        train = features[(features["target_day"] <= cutoff) & features["y"].notna()]
        if train["target_day"].nunique() < min_train_days:
            log.warning("Only %d training days before %s; skipping that block", train["target_day"].nunique(), block_start)
            block_start = block_end + timedelta(days=1)
            continue

        model = lgb.train(params, lgb.Dataset(train[cols], label=train["y"]), num_boost_round=NUM_BOOST_ROUND)
        importances.append(pd.Series(model.feature_importance("gain"), index=cols))

        block = features[(features["target_day"] >= block_start) & (features["target_day"] <= block_end)]
        predictions.append(
            pd.DataFrame(
                {
                    "target_day": block["target_day"].to_numpy(),
                    "hour": block["hour"].to_numpy(),
                    "model": MODEL_NAME,
                    "forecast": model.predict(block[cols]),
                    "actual": block["y"].to_numpy(),
                }
            )
        )
        log.info("%s: trained on %d days, forecast %s to %s", metric, train["target_day"].nunique(), block_start, block_end)
        block_start = block_end + timedelta(days=1)

    if not predictions:
        return pd.DataFrame(columns=["target_day", "hour", "model", "forecast", "actual"]), pd.DataFrame()

    importance = pd.concat(importances, axis=1).mean(axis=1)
    importance = (100 * importance / importance.sum()).sort_values(ascending=False)
    importance = pd.DataFrame({"feature": importance.index, "gain_pct": importance.to_numpy()})
    return pd.concat(predictions, ignore_index=True), importance


def load_weather(session, first: date, last: date) -> pd.DataFrame:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app import models

    start = datetime.combine(first - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(last + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc)
    rows = session.execute(
        select(models.Weather.ts, models.Weather.location, models.Weather.wind_100m, models.Weather.radiation, models.Weather.temp_2m).where(
            models.Weather.ts >= start, models.Weather.ts < end
        )
    ).all()
    return pd.DataFrame(rows, columns=["ts", "location", "wind_100m", "radiation", "temp_2m"])

