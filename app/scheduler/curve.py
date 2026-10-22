"""The hourly price + carbon curve the scheduler plans against.

Rule: use real data when it exists, forecasts when it doesn't.

  price   "published"  day-ahead prices are known for today, and for tomorrow after ~13:00
          "forecast"   anything further ahead
  carbon  "measured"   past hours, from the generation mix
          "forecast"   everything from now on (nobody publishes this in advance)

Every point carries its kind, so /jobs/{id}/explain can show exactly what the
decision was based on.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app import config
from app.forecast.baselines import LOCAL_TZ, forecast_day, local_hour_to_utc
from app.forecast.service import load_matrix
from app.queries import hourly_carbon, hourly_prices

REAL_KIND = {"price": "published", "carbon": "measured"}
COLUMN = {"price": "eur_mwh", "carbon": "g_per_kwh"}


def _local_days(start: datetime, end: datetime) -> list[date]:
    tz = ZoneInfo(LOCAL_TZ)
    first = start.astimezone(tz).date()
    last = (end - timedelta(seconds=1)).astimezone(tz).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _forecast_series(session, metric: str, days: list[date], model: str) -> pd.Series:
    matrix = load_matrix(session, metric, days[0] - timedelta(days=35), days[-1])
    points = {}
    for day in days:
        values = forecast_day(matrix, metric, day, model)
        for hour, value in enumerate(values):
            ts = local_hour_to_utc(day, hour)
            if ts is not None and not np.isnan(value):
                points[ts] = float(value)
    return pd.Series(points, dtype=float)


def build_curve(session, start: datetime, end: datetime, model: str | None = None) -> pd.DataFrame:
    """Hourly rows for [start, end): ts, eur_mwh, price_kind, g_per_kwh, carbon_kind."""
    model = model or config.FORECAST_MODEL
    hours = pd.date_range(pd.Timestamp(start).tz_convert("UTC"), pd.Timestamp(end).tz_convert("UTC"), freq="1h", inclusive="left")
    curve = pd.DataFrame(index=hours)
    days = _local_days(start, end)

    real = {
        "price": hourly_prices(session, start, end),
        "carbon": hourly_carbon(session, start, end),
    }
    for metric, frame in real.items():
        col, kind_col = COLUMN[metric], f"{metric}_kind"
        series = frame.set_index("ts")[col] if not frame.empty else pd.Series(dtype=float)
        series.index = pd.to_datetime(series.index, utc=True)
        curve[col] = series.reindex(hours)
        curve[kind_col] = np.where(curve[col].notna(), REAL_KIND[metric], None)

        if curve[col].isna().any():
            forecast = _forecast_series(session, metric, days, model).reindex(hours)
            # The repeated 02:00 on the autumn clock change has no wall-clock forecast of its own;
            # borrow the neighbouring hour rather than leave a hole.
            forecast = forecast.ffill(limit=1)
            fill = curve[col].isna() & forecast.notna()
            curve.loc[fill, col] = forecast[fill]
            curve.loc[fill, kind_col] = "forecast"

        curve[kind_col] = curve[kind_col].fillna("missing")

    curve.index.name = "ts"
    return curve.reset_index()[["ts", "eur_mwh", "price_kind", "g_per_kwh", "carbon_kind"]]
