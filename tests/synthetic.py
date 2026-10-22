"""Synthetic hourly data with a known daily + weekly shape, for testing forecasts."""

from datetime import date

import numpy as np
import pandas as pd

from app import models
from app.db import upsert

LOCAL_TZ = "Europe/Berlin"


def shape(local: pd.DatetimeIndex) -> np.ndarray:
    """A price-like day: cheap midday (solar), expensive evening, cheaper weekends."""
    hour = local.hour.to_numpy()
    weekend = (local.day_of_week.to_numpy() >= 5).astype(float)
    return 100 + 40 * np.cos((hour - 19) / 24 * 2 * np.pi) - 30 * np.exp(-((hour - 13) ** 2) / 8) - 20 * weekend


def hourly_frame(first: date, last: date, value_col: str, noise: float = 0.0, seed: int = 0) -> pd.DataFrame:
    """One row per real UTC hour from local midnight of `first` to the end of `last`."""
    start = pd.Timestamp(first, tz=LOCAL_TZ).tz_convert("UTC")
    end = pd.Timestamp(last, tz=LOCAL_TZ).tz_convert("UTC") + pd.Timedelta(days=1)
    ts = pd.date_range(start, end, freq="1h", inclusive="left")
    values = shape(ts.tz_convert(LOCAL_TZ))
    if noise:
        values = values + np.random.default_rng(seed).normal(0, noise, len(values))
    return pd.DataFrame({"ts": ts, value_col: values})


def seed_db(session, first: date, last: date, noise: float = 0.0) -> None:
    prices = hourly_frame(first, last, "eur_mwh", noise)
    carbon = hourly_frame(first, last, "g_per_kwh", noise, seed=1)
    upsert(
        session,
        models.Price,
        [{"ts": r.ts.to_pydatetime(), "source": "energy_charts", "eur_mwh": r.eur_mwh} for r in prices.itertuples()],
        ["ts", "source"],
    )
    upsert(
        session,
        models.CarbonIntensity,
        [{"ts": r.ts.to_pydatetime(), "g_per_kwh": r.g_per_kwh} for r in carbon.itertuples()],
        ["ts"],
    )
    session.commit()
