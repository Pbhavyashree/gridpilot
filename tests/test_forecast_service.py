from datetime import date

from fastapi.testclient import TestClient

from app.forecast.service import latest_forecast, make_and_store_forecast
from app.main import app
from tests.synthetic import seed_db


def test_store_and_read_forecast(session):
    seed_db(session, date(2026, 9, 1), date(2026, 10, 20))
    rows = make_and_store_forecast(session, "price", "profile_4w", date(2026, 10, 21))
    assert len(rows) == 24

    stored = latest_forecast(session, "price", "profile_4w")
    assert len(stored) == 24
    assert stored[0].target_ts.hour in (22, 23)  # local midnight in UTC (summer: 22:00)


def test_dst_target_day_has_25_hours(session):
    seed_db(session, date(2026, 9, 1), date(2026, 10, 24))
    rows = make_and_store_forecast(session, "price", "profile_4w", date(2026, 10, 25))
    # The wall-clock grid has 24 hours; the repeated 02:00 is stored once (the summer-time one).
    assert len(rows) == 24


def test_forecast_endpoint(session):
    with TestClient(app) as client:
        assert client.get("/forecast").status_code == 404

        seed_db(session, date(2026, 9, 1), date(2026, 10, 20))
        make_and_store_forecast(session, "carbon", "naive_week", date(2026, 10, 21))

        response = client.get("/forecast", params={"metric": "carbon", "model": "naive_week"})
    assert response.status_code == 200
    body = response.json()
    assert body["unit"] == "g/kWh"
    assert len(body["values"]) == 24
