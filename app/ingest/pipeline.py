"""Ingest pipeline: fetch -> transform -> upsert, with a fallback source for prices.

Every attempt is written to the ingest_runs table. That log is what the monitor
(Phase 5) will read to detect outages.
"""

import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

import httpx
import pandas as pd

from app import models
from app.db import upsert
from app.ingest import awattar, energy_charts, weather
from app.ingest.carbon import compute_carbon_intensity
from app.ingest.http import SourceUnavailable

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log_run(session, dataset, source, started_at, rows, status, error=None) -> None:
    session.add(
        models.IngestRun(
            started_at=started_at,
            finished_at=_now(),
            dataset=dataset,
            source=source,
            rows=rows,
            status=status,
            error=error,
        )
    )


def _records(df: pd.DataFrame, **extra) -> list[dict]:
    """DataFrame -> list of dicts with plain Python datetimes, ready for upsert."""
    out = []
    for row in df.to_dict("records"):
        row = {k: (v.to_pydatetime() if isinstance(v, pd.Timestamp) else v) for k, v in row.items()}
        row.update(extra)
        out.append(row)
    return out


def ingest_prices(session, client: httpx.Client, start: date, end: date, allow_fallback: bool = True) -> str:
    """Load prices for [start, end]. Returns the source that delivered them."""
    started = _now()
    try:
        df = energy_charts.fetch_prices(client, start, end)
        if df.empty:
            raise SourceUnavailable("Energy-Charts returned no price rows")
        n = upsert(session, models.Price, _records(df, source=energy_charts.SOURCE), ["ts", "source"])
        _log_run(session, "prices", energy_charts.SOURCE, started, n, "ok")
        session.commit()
        return energy_charts.SOURCE
    except SourceUnavailable as exc:
        session.rollback()
        _log_run(session, "prices", energy_charts.SOURCE, started, 0, "failed", str(exc))
        session.commit()
        if not allow_fallback:
            raise
        log.warning("Energy-Charts prices unavailable, falling back to aWATTar: %s", exc)

    started = _now()
    try:
        df = awattar.fetch_prices(
            client,
            datetime.combine(start, dtime.min, tzinfo=timezone.utc),
            datetime.combine(end + timedelta(days=1), dtime.min, tzinfo=timezone.utc),
        )
        n = upsert(session, models.Price, _records(df, source=awattar.SOURCE), ["ts", "source"])
        _log_run(session, "prices", awattar.SOURCE, started, n, "fallback")
        session.commit()
        return awattar.SOURCE
    except SourceUnavailable as exc:
        session.rollback()
        _log_run(session, "prices", awattar.SOURCE, started, 0, "failed", str(exc))
        session.commit()
        raise


def ingest_generation(session, client: httpx.Client, start: date, end: date) -> int:
    """Load the generation mix for [start, end] and derive carbon intensity from it."""
    started = _now()
    try:
        gen = energy_charts.fetch_generation(client, start, end)
    except SourceUnavailable as exc:
        _log_run(session, "generation", energy_charts.SOURCE, started, 0, "failed", str(exc))
        session.commit()
        raise

    n = upsert(session, models.Generation, _records(gen), ["ts", "fuel"])
    carbon = compute_carbon_intensity(gen)
    upsert(session, models.CarbonIntensity, _records(carbon), ["ts"])
    _log_run(session, "generation", energy_charts.SOURCE, started, n, "ok")
    session.commit()
    return n


def ingest_weather(session, client: httpx.Client, start: date, end: date, chunk_days: int = 90) -> int:
    """Weather forecasts for every location over [start, end], in chunks. Returns rows stored."""
    total = 0
    for name in weather.LOCATIONS:
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            started = _now()
            try:
                df = weather.fetch_location(client, name, cursor, chunk_end)
            except SourceUnavailable as exc:
                _log_run(session, "weather", f"{weather.SOURCE}:{name}", started, 0, "failed", str(exc))
                session.commit()
                raise
            n = upsert(session, models.Weather, _records(df), ["ts", "location"])
            _log_run(session, "weather", f"{weather.SOURCE}:{name}", started, n, "ok")
            session.commit()
            total += n
            cursor = chunk_end + timedelta(days=1)
    return total


def run_once(session, client: httpx.Client, today: date | None = None) -> None:
    """The hourly job: refresh the last 2 days plus tomorrow (published around midday)."""
    today = today or _now().date()
    start, end = today - timedelta(days=2), today + timedelta(days=1)

    for name, job in (
        ("prices", lambda: ingest_prices(session, client, start, end)),
        ("generation", lambda: ingest_generation(session, client, start, today)),
        ("weather", lambda: ingest_weather(session, client, start, today + timedelta(days=2))),
    ):
        try:
            job()
        except SourceUnavailable as exc:
            # Already logged in ingest_runs; keep going so one failure doesn't block the other.
            log.error("%s ingest failed: %s", name, exc)


def _backfill_range(session, client, start: date, end: date, prices: bool, generation: bool) -> None:
    log.info("Backfilling %s to %s", start, end)
    jobs = []
    if prices:
        jobs.append(lambda: ingest_prices(session, client, start, end, allow_fallback=False))
    if generation:
        jobs.append(lambda: ingest_generation(session, client, start, end))
    for job in jobs:
        try:
            job()
        except SourceUnavailable as exc:
            log.error("Chunk %s-%s failed, continuing: %s", start, end, exc)


def backfill(session, client: httpx.Client, days: int, pause_seconds: float = 3.0) -> None:
    """Load history in ~monthly chunks, oldest first. No aWATTar fallback (rate limits)."""
    end = _now().date()
    cursor = end - timedelta(days=days)
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=29), end)
        _backfill_range(session, client, cursor, chunk_end, prices=True, generation=True)
        cursor = chunk_end + timedelta(days=1)
        time.sleep(pause_seconds)


def backfill_missing(session, client: httpx.Client, days: int, pause_seconds: float = 3.0) -> dict[str, int]:
    """Re-fetch only the days that are incomplete. Returns how many days were missing per dataset."""
    from app.ingest.coverage import find_gaps, to_ranges

    gaps = find_gaps(session, days)
    for dataset, generation in (("prices", False), ("carbon", True)):
        for start, end in to_ranges(gaps[dataset]):
            # One extra day on each side covers the UTC/Berlin offset at the edges.
            _backfill_range(
                session,
                client,
                start - timedelta(days=1),
                end + timedelta(days=1),
                prices=not generation,
                generation=generation,
            )
            time.sleep(pause_seconds)
    return {name: len(missing) for name, missing in gaps.items()}
