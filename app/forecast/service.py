"""Glue between the database and the forecast models."""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from app import models
from app.db import upsert
from app.forecast.baselines import (
    METRIC_COLUMNS,
    forecast_day,
    local_hour_to_utc,
    to_day_hour_matrix,
)
from app.queries import hourly_carbon, hourly_prices

LOADERS = {"price": hourly_prices, "carbon": hourly_carbon}


def load_matrix(session, metric: str, first_day: date, last_day: date) -> pd.DataFrame:
    start = datetime.combine(first_day - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(last_day + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc)
    hourly = LOADERS[metric](session, start, end)
    return to_day_hour_matrix(hourly, METRIC_COLUMNS[metric])


def make_and_store_forecast(session, metric: str, model: str, target_day: date) -> list[dict]:
    """Forecast one target day and save it. Returns the stored rows."""
    matrix = load_matrix(session, metric, target_day - timedelta(days=35), target_day)
    values = forecast_day(matrix, metric, target_day, model)
    made_at = datetime.now(timezone.utc).replace(microsecond=0)

    rows = []
    for hour, value in enumerate(values):
        ts = local_hour_to_utc(target_day, hour)
        if ts is None or np.isnan(value):
            continue
        rows.append(
            {"made_at": made_at, "target_ts": ts.to_pydatetime(), "metric": metric, "model": model, "value": float(value)}
        )
    upsert(session, models.Forecast, rows, ["made_at", "target_ts", "metric", "model"])
    session.commit()
    return rows


def latest_forecast(session, metric: str, model: str) -> list[models.Forecast]:
    latest = session.scalar(
        select(func.max(models.Forecast.made_at)).where(models.Forecast.metric == metric, models.Forecast.model == model)
    )
    if latest is None:
        return []
    return list(
        session.scalars(
            select(models.Forecast)
            .where(models.Forecast.metric == metric, models.Forecast.model == model, models.Forecast.made_at == latest)
            .order_by(models.Forecast.target_ts)
        )
    )
