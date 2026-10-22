"""Read helpers. The ONE place where native-resolution data becomes hourly data."""

from datetime import datetime

import pandas as pd
from sqlalchemy import select

from app import models

# When two sources have the same timestamp, prefer the primary one.
SOURCE_PRIORITY = {"energy_charts": 0, "awattar": 1}


def hourly_prices(session, start: datetime, end: datetime) -> pd.DataFrame:
    rows = session.execute(
        select(models.Price.ts, models.Price.source, models.Price.eur_mwh).where(
            models.Price.ts >= start, models.Price.ts < end
        )
    ).all()
    if not rows:
        return pd.DataFrame(columns=["ts", "eur_mwh", "source"])

    df = pd.DataFrame(rows, columns=["ts", "source", "eur_mwh"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["priority"] = df["source"].map(SOURCE_PRIORITY).fillna(99)
    df = df.sort_values(["ts", "priority"]).drop_duplicates("ts", keep="first")

    hourly = (
        df.set_index("ts")
        .resample("1h")
        .agg({"eur_mwh": "mean", "source": "first"})
        .dropna(subset=["eur_mwh"])
        .reset_index()
    )
    return hourly


def hourly_carbon(session, start: datetime, end: datetime) -> pd.DataFrame:
    rows = session.execute(
        select(models.CarbonIntensity.ts, models.CarbonIntensity.g_per_kwh).where(
            models.CarbonIntensity.ts >= start, models.CarbonIntensity.ts < end
        )
    ).all()
    if not rows:
        return pd.DataFrame(columns=["ts", "g_per_kwh"])

    df = pd.DataFrame(rows, columns=["ts", "g_per_kwh"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").resample("1h").mean().dropna().reset_index()
