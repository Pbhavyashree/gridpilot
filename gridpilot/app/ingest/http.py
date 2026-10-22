"""Small HTTP helper: GET JSON with timeouts and retries with exponential backoff."""

import logging
import time

import httpx

from app import config

log = logging.getLogger(__name__)


class SourceUnavailable(Exception):
    """Raised when a data source fails after all retries."""


def get_json(
    client: httpx.Client,
    url: str,
    params: dict | None = None,
    retries: int | None = None,
    backoff_seconds: float = 2.0,
) -> dict:
    retries = config.HTTP_RETRIES if retries is None else retries
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            log.warning("GET %s failed (attempt %d/%d): %s", url, attempt, retries, exc)
            if attempt < retries:
                time.sleep(backoff_seconds * 2 ** (attempt - 1))

    raise SourceUnavailable(f"{url} failed after {retries} attempts: {last_error}")


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=config.HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": "GridPilot/0.1 (portfolio project)"},
    )
