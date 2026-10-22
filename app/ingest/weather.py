"""Weather FORECASTS from Open-Meteo's Previous Model Runs API (free, no key).
https://open-meteo.com/en/docs/previous-runs-api

Why "previous runs" and not plain historical weather?
In the backtest we must only use what was known at 10:00 the day before. Real
historical weather would tell the model tomorrow's wind perfectly, which is a hidden
leak that makes results look far better than they could ever be live.

Open-Meteo archives what the weather models PREDICTED. A variable with the suffix
`_previous_day2` is the value forecast about 48 hours before the valid time. Our
forecasts are made 14 to 37 hours ahead, so day-2 forecasts are always available in
time. They are a bit staler than what we'd really have, which makes the backtest
slightly pessimistic rather than optimistic: the safe direction.

Archived from January 2024 for most weather models.
"""

from datetime import date

import httpx
import pandas as pd

from app import config
from app.ingest.http import get_json

SOURCE = "open_meteo"

# A handful of points that drive German wind and solar output.
LOCATIONS: dict[str, tuple[float, float]] = {
    "north_sea": (54.5, 8.9),  # Schleswig-Holstein coast, onshore and near offshore wind
    "baltic": (54.1, 12.1),  # Mecklenburg-Vorpommern, wind
    "brandenburg": (52.3, 14.0),  # big onshore wind region
    "central": (51.3, 9.5),  # Hesse / Lower Saxony
    "bavaria": (48.4, 11.0),  # most solar capacity
    "southwest": (48.6, 8.9),  # Baden-Württemberg, solar
}

# Our column name -> Open-Meteo variable name (without the lead-time suffix)
VARIABLES = {
    "wind_100m": "wind_speed_100m",  # m/s at hub height
    "radiation": "shortwave_radiation",  # W/m², drives solar output
    "temp_2m": "temperature_2m",  # °C, drives heating demand
}


def _api_vars(lead_days: int) -> dict[str, str]:
    return {ours: f"{theirs}_previous_day{lead_days}" for ours, theirs in VARIABLES.items()}


def fetch_location(
    client: httpx.Client, name: str, start: date, end: date, lead_days: int | None = None
) -> pd.DataFrame:
    """Hourly weather forecasts for one location. Columns: ts, location, wind_100m, radiation, temp_2m."""
    lead_days = config.WEATHER_LEAD_DAYS if lead_days is None else lead_days
    lat, lon = LOCATIONS[name]
    api_vars = _api_vars(lead_days)
    data = get_json(
        client,
        f"{config.OPEN_METEO_BASE_URL}/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(api_vars.values()),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timezone": "GMT",
            "timeformat": "unixtime",
            "wind_speed_unit": "ms",
        },
    )
    return parse(data, name, api_vars)


def parse(data: dict | list, name: str, api_vars: dict[str, str]) -> pd.DataFrame:
    if isinstance(data, list):  # the API answers with a list when several points are requested
        data = data[0]
    hourly = data.get("hourly", {})
    frame = pd.DataFrame({"ts": pd.to_datetime(hourly.get("time", []), unit="s", utc=True)})
    for ours, theirs in api_vars.items():
        values = hourly.get(theirs)
        frame[ours] = pd.Series(values, dtype=float) if values is not None else float("nan")
    frame["location"] = name
    # Hours the model hadn't forecast yet come back as null; keep rows that have at least something.
    frame = frame.dropna(subset=list(api_vars), how="all")
    return frame[["ts", "location", *api_vars]].reset_index(drop=True)
