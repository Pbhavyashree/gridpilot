"""Find gaps in the loaded data, so a backfill can re-fetch only what's missing.

Days are Berlin calendar days. A day is complete when it has a value for every
hour it actually has: 24 normally, 23 on the spring DST change, 25 in autumn.
"""

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from app.queries import hourly_carbon, hourly_prices

LOCAL_TZ = "Europe/Berlin"


def expected_hours(day: date) -> int:
    start = pd.Timestamp(day, tz=LOCAL_TZ)
    end = pd.Timestamp(day + timedelta(days=1), tz=LOCAL_TZ)
    return int((end - start) / pd.Timedelta(hours=1))


def daily_coverage(hourly: pd.DataFrame) -> pd.Series:
    """Hourly frame with a ts column -> count of hours per local date."""
    if hourly.empty:
        return pd.Series(dtype=int)
    local_dates = pd.to_datetime(hourly["ts"], utc=True).dt.tz_convert(LOCAL_TZ).dt.date
    return local_dates.value_counts().sort_index()


def incomplete_days(hourly: pd.DataFrame, first: date, last: date) -> list[date]:
    counts = daily_coverage(hourly)
    missing = []
    day = first
    while day <= last:
        if counts.get(day, 0) < expected_hours(day):
            missing.append(day)
        day += timedelta(days=1)
    return missing


def find_gaps(session, days: int, today: date | None = None) -> dict[str, list[date]]:
    """Incomplete days per dataset over the last `days` days, up to yesterday."""
    today = today or datetime.now(timezone.utc).date()
    first, last = today - timedelta(days=days), today - timedelta(days=1)
    start = datetime.combine(first - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return {
        "prices": incomplete_days(hourly_prices(session, start, end), first, last),
        "carbon": incomplete_days(hourly_carbon(session, start, end), first, last),
    }


def to_ranges(days: list[date], max_len: int = 29) -> list[tuple[date, date]]:
    """[d1, d2, d3, d7, d8] -> [(d1, d3), (d7, d8)], each range at most max_len days."""
    ranges: list[tuple[date, date]] = []
    for day in sorted(days):
        if ranges and day - ranges[-1][1] == timedelta(days=1) and (day - ranges[-1][0]).days < max_len:
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges
