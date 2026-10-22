"""The hourly price + carbon curve the scheduler plans against.

Rule: use real data when it exists, forecasts when it doesn't.

  price   "published"  day-ahead prices are known for today, and for tomorrow after ~13:00
          "forecast"   anything further ahead
  carbon  "measured"   past hours, from the generation mix
          "forecast"   everything from now on (nobody publishes this in advance)

Every point carries its kind, and the curve records which forecast model was really
used per metric (lgbm, or profile_4w when no trained model exists yet), so
/jobs/{id}/explain can show exactly what the decision was based on.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app import config
from app.forecast.baselines import LOCAL_TZ, local_hour_to_utc
from app.forecast.live import forecast_values
from app.forecast.service import load_matrix
from app.queries import hourly_carbon, hourly_prices

REAL_KIND = {"price": "published", "carbon": "measured"}
COLUMN = {"price": "eur_mwh", "carbon": "g_per_kwh"}


def _local_days(start: datetime, end: datetime) -> list[date]:
    tz = ZoneInfo(LOCAL_TZ)
    first = start.astimezone(tz).date()
    last = (end - timedelta(seconds=1)).astimezone(tz).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _forecast_series(session, metric: str, days: list[date], model: str) -> tuple[pd.Series, set[str]]:
    matrix = load_matrix(session, metric, days[0] - timedelta(days=35), days[-1])
    points, used = {}, set()
    for day in days:
        values, model_used = forecast_values(session, metric, model, day, matrix=matrix)
        used.add(model_used)
        for hour, value in enumerate(values):
            ts = local_hour_to_utc(day, hour)
            if ts is not None and not np.isnan(value):
                points[ts] = float(value)
    return pd.Series(points, dtype=float), used


def build_curve(session, start: datetime, end: datetime, model: str | None = None) -> pd.DataFrame:
    """Hourly rows for [start, end): ts, eur_mwh, price_kind, g_per_kwh, carbon_kind.

    curve.attrs["models_used"] says which forecast model filled the forecast hours, per metric.
    """
    model = model or config.FORECAST_MODEL
    hours = pd.date_range(pd.Timestamp(start).tz_convert("UTC"), pd.Timestamp(end).tz_convert("UTC"), freq="1h", inclusive="left")
    curve = pd.DataFrame(index=hours)
    days = _local_days(start, end)
    models_used: dict[str, str | None] = {}

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
        models_used[metric] = None

        if curve[col].isna().any():
            forecast, used = _forecast_series(session, metric, days, model)
            forecast = forecast.reindex(hours)
            # The repeated 02:00 on the autumn clock change has no wall-clock forecast of its own;
            # borrow the neighbouring hour rather than leave a hole.
            forecast = forecast.ffill(limit=1)
            fill = curve[col].isna() & forecast.notna()
            curve.loc[fill, col] = forecast[fill]
            curve.loc[fill, kind_col] = "forecast"
            models_used[metric] = ", ".join(sorted(used))

        curve[kind_col] = curve[kind_col].fillna("missing")

    curve.index.name = "ts"
    out = curve.reset_index()[["ts", "eur_mwh", "price_kind", "g_per_kwh", "carbon_kind"]]
    out.attrs["models_used"] = models_used
    return out
