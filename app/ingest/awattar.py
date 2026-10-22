"""Client for the aWATTar market data API (https://www.awattar.de/services/api).

Used as the FALLBACK price source when Energy-Charts is down.
Fair-use rate limit applies: never poll it in a tight loop.
"""

from datetime import datetime, timezone

import httpx
import pandas as pd

from app import config
from app.ingest.http import get_json

SOURCE = "awattar"


def fetch_prices(client: httpx.Client, start: datetime, end: datetime) -> pd.DataFrame:
    """Day-ahead prices in EUR/MWh. Returns columns: ts (UTC), eur_mwh."""
    data = get_json(
        client,
        f"{config.AWATTAR_BASE_URL}/v1/marketdata",
        params={"start": _to_ms(start), "end": _to_ms(end)},
    )
    return parse_prices(data)


def parse_prices(data: dict) -> pd.DataFrame:
    rows = data.get("data", [])
    df = pd.DataFrame(
        {
            "ts": pd.to_datetime([r["start_timestamp"] for r in rows], unit="ms", utc=True),
            "eur_mwh": [r["marketprice"] for r in rows],
        }
    )
    return df.dropna().reset_index(drop=True)


def _to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)
