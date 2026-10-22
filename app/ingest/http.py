"""Small HTTP helper: GET JSON with timeouts and retries with exponential backoff.

Rate limits (HTTP 429) get special treatment: more attempts and longer waits,
and the server's Retry-After header is respected when it sends one.
"""

import logging
import time

import httpx

from app import config

log = logging.getLogger(__name__)

RATE_LIMIT_RETRIES = 6
RATE_LIMIT_BASE_WAIT_SECONDS = 10.0
MAX_WAIT_SECONDS = 120.0


class SourceUnavailable(Exception):
    """Raised when a data source fails after all retries."""


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return min(float(value), MAX_WAIT_SECONDS)
    except ValueError:
        return None  # an HTTP date instead of seconds; fall back to our own backoff


def get_json(
    client: httpx.Client,
    url: str,
    params: dict | None = None,
    retries: int | None = None,
    backoff_seconds: float = 2.0,
    sleep=time.sleep,
) -> dict:
    retries = config.HTTP_RETRIES if retries is None else retries
    attempt = 0
    rate_limited = 0
    last_error: Exception | None = None

    while True:
        attempt += 1
        try:
            response = client.get(url, params=params)
            if response.status_code == 429:
                rate_limited += 1
                if rate_limited > RATE_LIMIT_RETRIES:
                    raise SourceUnavailable(f"{url} still rate-limited after {RATE_LIMIT_RETRIES} retries")
                wait = _retry_after_seconds(response) or min(
                    RATE_LIMIT_BASE_WAIT_SECONDS * 2 ** (rate_limited - 1), MAX_WAIT_SECONDS
                )
                log.warning("Rate-limited by %s, waiting %.0fs (%d/%d)", url, wait, rate_limited, RATE_LIMIT_RETRIES)
                sleep(wait)
                attempt -= 1  # rate limits don't use up the normal retry budget
                continue
            response.raise_for_status()
            return response.json()
        except SourceUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            log.warning("GET %s failed (attempt %d/%d): %s", url, attempt, retries, exc)
            if attempt >= retries:
                raise SourceUnavailable(f"{url} failed after {retries} attempts: {last_error}") from exc
            sleep(backoff_seconds * 2 ** (attempt - 1))


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=config.HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": "GridPilot/0.1 (portfolio project)"},
    )
