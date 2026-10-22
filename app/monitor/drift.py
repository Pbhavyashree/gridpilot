"""Forecast drift: is the live forecast getting worse?

Every morning the scheduler worker stores the day-ahead forecast it planned with
(the `forecasts` table). Once the real values are in, each day gets an error score
(MAE). Drift = the last few days' error is far above what's normal for this model,
measured with a robust z-score against the weeks before.

Typical causes: a weather pattern the model never saw, a market change (new price
cap, more batteries), or a broken input such as missing weather data.

It needs about 2.5 weeks of stored forecasts before it can judge. Until then it stays quiet.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import select

from app import config, models
from app.forecast.baselines import LOCAL_TZ, METRIC_UNITS
from app.monitor.checks import Finding, robust_z
from app.queries import hourly_carbon, hourly_prices

RECENT_DAYS = 3
MIN_HISTORY_DAYS = 14
Z_ALERT = 3.0
BERLIN = ZoneInfo(LOCAL_TZ)
ACTUALS = {"price": (hourly_prices, "eur_mwh"), "carbon": (hourly_carbon, "g_per_kwh")}


def daily_errors(session, metric: str, model: str, now: datetime, days: int = 45) -> pd.Series:
    """MAE per Berlin day of the day-ahead forecast (the latest one made before that day began)."""
    start = now - timedelta(days=days + 1)
    rows = session.execute(
        select(models.Forecast.made_at, models.Forecast.target_ts, models.Forecast.value).where(
            models.Forecast.metric == metric, models.Forecast.model.like(f"{model}%"), models.Forecast.target_ts >= start
        )
    ).all()
    if not rows:
        return pd.Series(dtype=float)

    fc = pd.DataFrame(rows, columns=["made_at", "target_ts", "value"])
    fc["made_at"] = pd.to_datetime(fc["made_at"], utc=True)
    fc["target_ts"] = pd.to_datetime(fc["target_ts"], utc=True)
    fc["day"] = fc["target_ts"].dt.tz_convert(LOCAL_TZ).dt.date
    day_start = pd.to_datetime(fc["day"].astype(str)).dt.tz_localize(LOCAL_TZ).dt.tz_convert("UTC")
    fc = fc[fc["made_at"] < day_start]  # day-ahead forecasts only
    if fc.empty:
        return pd.Series(dtype=float)
    fc = fc.sort_values("made_at").groupby("target_ts").last().reset_index()

    loader, column = ACTUALS[metric]
    actual = loader(session, fc["target_ts"].min().to_pydatetime(), fc["target_ts"].max().to_pydatetime() + timedelta(hours=1))
    if actual.empty:
        return pd.Series(dtype=float)
    actual["ts"] = pd.to_datetime(actual["ts"], utc=True)
    merged = fc.merge(actual[["ts", column]], left_on="target_ts", right_on="ts")
    merged["abs_err"] = (merged["value"] - merged[column]).abs()
    per_day = merged.groupby("day").agg(mae=("abs_err", "mean"), hours=("abs_err", "size"))
    return per_day[per_day["hours"] >= 20]["mae"]  # only (nearly) complete days


def drift_status(errors: pd.Series) -> tuple[float, float, float] | None:
    """(recent mean error, normal error, z) or None if there isn't enough history yet."""
    if len(errors) < MIN_HISTORY_DAYS + RECENT_DAYS:
        return None
    recent = errors.iloc[-RECENT_DAYS:]
    history = errors.iloc[:-RECENT_DAYS]
    value = float(recent.mean())
    return value, float(np.median(history)), robust_z(value, history.tolist())


def check_drift(session, now: datetime) -> list[Finding]:
    model = config.FORECAST_MODEL
    findings = []
    for metric in ("price", "carbon"):
        errors = daily_errors(session, metric, model, now)
        status = drift_status(errors)
        if status is None:
            continue
        recent, normal, z = status
        if z > Z_ALERT:
            unit = METRIC_UNITS[metric]
            recent_days = [f"{day}: {value:.1f} {unit}" for day, value in errors.iloc[-RECENT_DAYS:].items()]
            findings.append(
                Finding(
                    key=f"drift:{metric}",
                    kind="forecast_drift",
                    severity="warning",
                    title=f"{metric.title()} forecast is drifting ({recent / normal:.1f}x its usual error)",
                    summary=f"Over the last {RECENT_DAYS} days the {model} {metric} forecast was off by {recent:.1f} {unit} "
                    f"on average; normally it's about {normal:.1f} {unit} (robust z-score {z:.1f}).",
                    evidence=recent_days,
                    action_taken="Scheduling continues; drifted forecasts mostly cost some savings, not deadlines.",
                    next_step="Check the weather feed first (a broken input is the most common cause), then retrain: "
                    "submit a `retrain_models` job or run `python -m app.forecast.cli train`.",
                )
            )
    return findings


def recent_mae(session, metric: str, now: datetime, days: int = 7) -> float | None:
    """Mean daily error over the last `days` days, for the dashboard."""
    errors = daily_errors(session, metric, config.FORECAST_MODEL, now, days=days + 2)
    errors = errors[[d >= (now.astimezone(BERLIN).date() - timedelta(days=days)) for d in errors.index]]
    return float(errors.mean()) if len(errors) else None
