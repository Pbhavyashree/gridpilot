from datetime import date

import httpx
import pytest

from app.ingest import http
from app.ingest.coverage import expected_hours, find_gaps, to_ranges
from app.ingest.http import SourceUnavailable, get_json
from tests.synthetic import seed_db


def test_expected_hours_on_dst_days():
    assert expected_hours(date(2026, 3, 29)) == 23
    assert expected_hours(date(2026, 10, 25)) == 25
    assert expected_hours(date(2026, 10, 20)) == 24


def test_to_ranges_groups_consecutive_days():
    days = [date(2026, 1, d) for d in (1, 2, 3, 7, 8, 20)]
    assert to_ranges(days) == [
        (date(2026, 1, 1), date(2026, 1, 3)),
        (date(2026, 1, 7), date(2026, 1, 8)),
        (date(2026, 1, 20), date(2026, 1, 20)),
    ]


def test_find_gaps_spots_missing_days(session):
    seed_db(session, date(2026, 9, 1), date(2026, 9, 10))
    seed_db(session, date(2026, 9, 14), date(2026, 9, 30))
    gaps = find_gaps(session, days=29, today=date(2026, 10, 1))  # checks 2 Sep .. 30 Sep
    assert gaps["prices"] == [date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13)]
    assert gaps["carbon"] == gaps["prices"]


def _client(responses: list[httpx.Response]) -> httpx.Client:
    queue = iter(responses)
    return httpx.Client(transport=httpx.MockTransport(lambda request: next(queue)))


def test_rate_limit_waits_and_recovers():
    waits = []
    client = _client(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert get_json(client, "https://example.test", retries=1, sleep=waits.append) == {"ok": True}
    assert waits == [7.0, http.RATE_LIMIT_BASE_WAIT_SECONDS * 2]  # Retry-After respected, then backoff


def test_rate_limit_gives_up_eventually():
    client = _client([httpx.Response(429)] * (http.RATE_LIMIT_RETRIES + 1))
    with pytest.raises(SourceUnavailable):
        get_json(client, "https://example.test", sleep=lambda _: None)


def test_server_error_uses_normal_retries():
    client = _client([httpx.Response(503), httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    assert get_json(client, "https://example.test", retries=3, sleep=lambda _: None) == {"ok": 1}
