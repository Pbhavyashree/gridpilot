"""Client for the Energy-Charts API by Fraunhofer ISE (https://api.energy-charts.info).

Free, no registration. Data attribution: "Energy-Charts / Fraunhofer ISE".
Responses use parallel arrays: a list of unix timestamps plus value lists of the same length.
"""

from datetime import date

import httpx
import pandas as pd

from app import config
from app.ingest.http import get_json

SOURCE = "energy_charts"

# Series in /public_power that are not generation (loads, shares, trade, storage charging).
# They must not count towards the generation mix.
_NOT_GENERATION_KEYWORDS = ("load", "share", "trading", "consumption", "import", "export")


def fetch_prices(client: httpx.Client, start: date, end: date, bzn: str | None = None) -> pd.DataFrame:
    """Day-ahead prices in EUR/MWh. Returns columns: ts (UTC), eur_mwh."""
    data = get_json(
        client,
        f"{config.ENERGY_CHARTS_BASE_URL}/price",
        params={"bzn": bzn or config.BIDDING_ZONE, "start": start.isoformat(), "end": end.isoformat()},
    )
    return parse_prices(data)


def parse_prices(data: dict) -> pd.DataFrame:
    df = pd.DataFrame({"unix": data.get("unix_seconds", []), "eur_mwh": data.get("price", [])})
    df = df.dropna()
    df["ts"] = pd.to_datetime(df["unix"], unit="s", utc=True)
    return df[["ts", "eur_mwh"]].reset_index(drop=True)


def fetch_generation(client: httpx.Client, start: date, end: date, country: str | None = None) -> pd.DataFrame:
    """Public net generation by source in MW. Returns long format: ts, fuel, mw."""
    data = get_json(
        client,
        f"{config.ENERGY_CHARTS_BASE_URL}/public_power",
        params={"country": country or config.COUNTRY, "start": start.isoformat(), "end": end.isoformat()},
    )
    return parse_generation(data)


def parse_generation(data: dict) -> pd.DataFrame:
    timestamps = pd.to_datetime(data.get("unix_seconds", []), unit="s", utc=True)
    frames = []
    for series in data.get("production_types", []):
        name = series.get("name", "")
        if any(word in name.lower() for word in _NOT_GENERATION_KEYWORDS):
            continue
        values = series.get("data") or []
        if len(values) != len(timestamps):
            continue  # malformed series; skip rather than misalign timestamps
        frames.append(pd.DataFrame({"ts": timestamps, "fuel": name, "mw": values}))

    if not frames:
        return pd.DataFrame(columns=["ts", "fuel", "mw"])

    df = pd.concat(frames, ignore_index=True).dropna()
    # Negative values here are storage charging, not generation.
    df = df[df["mw"] >= 0]
    return df.reset_index(drop=True)
