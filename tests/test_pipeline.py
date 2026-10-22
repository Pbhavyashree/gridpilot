from datetime import date, datetime, timezone

import httpx
from sqlalchemy import select

from app import models
from app.ingest.pipeline import run_once
from app.queries import hourly_prices
from tests.fixtures import AWATTAR_MARKETDATA, ENERGY_CHARTS_POWER, ENERGY_CHARTS_PRICE


def make_client(energy_charts_price_status: int = 200) -> httpx.Client:
    """An httpx client whose requests are answered by fake handlers, no network needed."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.energy-charts.info" and request.url.path == "/price":
            if energy_charts_price_status != 200:
                return httpx.Response(energy_charts_price_status, text="No server is available")
            return httpx.Response(200, json=ENERGY_CHARTS_PRICE)
        if request.url.host == "api.energy-charts.info" and request.url.path == "/public_power":
            return httpx.Response(200, json=ENERGY_CHARTS_POWER)
        if request.url.host == "api.awattar.de":
            return httpx.Response(200, json=AWATTAR_MARKETDATA)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_happy_path_uses_energy_charts(session):
    run_once(session, make_client(), today=date(2026, 10, 20))

    sources = set(session.scalars(select(models.Price.source)).all())
    assert sources == {"energy_charts"}
    statuses = {(r.dataset, r.status) for r in session.scalars(select(models.IngestRun))}
    assert statuses == {("prices", "ok"), ("generation", "ok")}
    assert session.scalars(select(models.CarbonIntensity)).first() is not None


def test_outage_falls_back_to_awattar_and_logs_it(session):
    run_once(session, make_client(energy_charts_price_status=503), today=date(2026, 10, 20))

    sources = set(session.scalars(select(models.Price.source)).all())
    assert sources == {"awattar"}
    runs = [(r.source, r.status) for r in session.scalars(select(models.IngestRun).where(models.IngestRun.dataset == "prices"))]
    assert ("energy_charts", "failed") in runs
    assert ("awattar", "fallback") in runs


def test_rerun_is_idempotent(session):
    client = make_client()
    run_once(session, client, today=date(2026, 10, 20))
    run_once(session, client, today=date(2026, 10, 20))
    assert len(session.scalars(select(models.Price)).all()) == 4


def test_quarter_hour_prices_resample_to_hourly_mean(session):
    run_once(session, make_client(), today=date(2026, 10, 20))
    df = hourly_prices(
        session,
        datetime(2026, 10, 20, tzinfo=timezone.utc),
        datetime(2026, 10, 21, tzinfo=timezone.utc),
    )
    assert len(df) == 1
    assert df["eur_mwh"].iloc[0] == 95.0  # mean of 80, 90, 100, 110
    assert df["source"].iloc[0] == "energy_charts"
