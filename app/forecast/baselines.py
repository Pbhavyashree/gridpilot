"""Baseline forecasts for tomorrow's 24 hours of price or carbon intensity.

Everything works on a "day x hour" grid in Berlin local time: one row per calendar
day, one column per wall-clock hour 0..23. That makes "same hour last week" mean
the same wall-clock hour even across a daylight-saving change.

  - 23-hour spring day: hour 2 doesn't exist, so it's NaN.
  - 25-hour autumn day: hour 2 happens twice, so it's the mean of both.

WHAT IS KNOWN WHEN (the most important rule in this file):
We forecast at 10:00 Berlin time on the "issue day" D, for target day T = D + 1.
  - Prices for D were published on D - 1 around 13:00, so prices are known through D.
  - Carbon comes from measured generation, so day D is still in progress at 10:00.
    The last COMPLETE carbon day is D - 1.
Every model only ever receives rows up to `known_through()`. That slice is what
keeps the backtest honest (no peeking at the future).
"""

import warnings
from collections.abc import Callable
from datetime import date, timedelta

import numpy as np
import pandas as pd

LOCAL_TZ = "Europe/Berlin"
HOURS = list(range(24))
ISSUE_HOUR_LOCAL = 10

METRIC_COLUMNS = {"price": "eur_mwh", "carbon": "g_per_kwh"}
METRIC_UNITS = {"price": "EUR/MWh", "carbon": "g/kWh"}


def to_day_hour_matrix(hourly: pd.DataFrame, value_col: str) -> pd.DataFrame:
    """Hourly frame (ts in UTC, value_col) -> DataFrame indexed by local date, columns 0..23."""
    if hourly.empty:
        return pd.DataFrame(columns=HOURS, dtype=float)
    local = pd.to_datetime(hourly["ts"], utc=True).dt.tz_convert(LOCAL_TZ)
    frame = pd.DataFrame({"day": local.dt.date, "hour": local.dt.hour, "value": hourly[value_col].astype(float)})
    matrix = frame.pivot_table(index="day", columns="hour", values="value", aggfunc="mean")
    return matrix.reindex(columns=HOURS).sort_index()


def known_through(metric: str, issue_day: date) -> date:
    if metric == "price":
        return issue_day
    if metric == "carbon":
        return issue_day - timedelta(days=1)
    raise ValueError(f"unknown metric: {metric}")


def _row(known: pd.DataFrame, day: date) -> np.ndarray:
    if day in known.index:
        return known.loc[day].to_numpy(dtype=float)
    return np.full(24, np.nan)


# Each model: (known history, target day) -> array of 24 values (NaN where it can't say).


def naive_day(known: pd.DataFrame, target_day: date) -> np.ndarray:
    """Same hour on the most recent known day."""
    if known.empty:
        return np.full(24, np.nan)
    return known.iloc[-1].to_numpy(dtype=float)


def naive_week(known: pd.DataFrame, target_day: date) -> np.ndarray:
    """Same hour, same weekday, one week earlier."""
    return _row(known, target_day - timedelta(days=7))


def profile_4w(known: pd.DataFrame, target_day: date) -> np.ndarray:
    """Mean of the same hour and weekday over the last 4 weeks (needs at least 2 of them)."""
    rows = np.vstack([_row(known, target_day - timedelta(days=7 * k)) for k in range(1, 5)])
    counts = np.sum(~np.isnan(rows), axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # "mean of empty slice" for all-NaN hours
        mean = np.nanmean(rows, axis=0)
    return np.where(counts >= 2, mean, np.nan)


MODELS: dict[str, Callable[[pd.DataFrame, date], np.ndarray]] = {
    "naive_day": naive_day,
    "naive_week": naive_week,
    "profile_4w": profile_4w,
}


def forecast_day(matrix: pd.DataFrame, metric: str, target_day: date, model: str) -> np.ndarray:
    """Forecast `target_day` using only what was known at 10:00 the day before."""
    cutoff = known_through(metric, target_day - timedelta(days=1))
    known = matrix.loc[matrix.index <= cutoff]
    return MODELS[model](known, target_day)


def local_hour_to_utc(day: date, hour: int) -> pd.Timestamp | None:
    """Wall-clock hour on a Berlin day -> UTC start time. None for the hour that doesn't exist."""
    ts = pd.Timestamp(day) + pd.Timedelta(hours=hour)
    localized = pd.DatetimeIndex([ts]).tz_localize(LOCAL_TZ, ambiguous=np.array([True]), nonexistent="NaT")
    if localized.isna()[0]:
        return None
    return localized[0].tz_convert("UTC")
