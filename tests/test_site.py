from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import site
from app.main import app
from tests.synthetic import seed_db


@pytest.fixture
def client(session):
    site._cache.clear()
    today = datetime.now(timezone.utc).date()
    seed_db(session, today - timedelta(days=40), today + timedelta(days=1))
    with TestClient(app) as c:
        yield c


def test_front_page_and_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "GridPilot" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/fonts/barlow-latin-400-normal.woff2").status_code == 200
    assert "http" not in page.text.split("<body>")[0].replace("http://www.w3.org", "")  # nothing loaded from elsewhere


def test_overview_has_everything_the_page_needs(client):
    body = client.get("/overview").json()
    assert len(body["curve"]) == site.CURVE_HOURS
    assert {"ts", "eur_mwh", "price_kind", "g_per_kwh", "carbon_kind"} <= set(body["curve"][0])
    assert {"open", "recently_resolved", "feeds"} <= set(body["health"])
    assert isinstance(body["jobs"], list) and isinstance(body["models"], list)


def test_preview_answers_without_saving_a_job(client):
    response = client.post("/plan/preview", json={"duration_hours": 2, "within_hours": 24, "objective": "carbon"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chosen"]["co2_kg"] <= body["run_now"]["co2_kg"]
    assert client.get("/jobs").json() == []  # nothing was created


def test_preview_rejects_impossible_jobs(client):
    response = client.post("/plan/preview", json={"duration_hours": 8, "within_hours": 6})
    assert response.status_code == 422
    assert "longer than" in response.json()["detail"]


def test_curve_is_cached(client, monkeypatch):
    client.get("/overview")
    calls = []
    monkeypatch.setattr(site, "build_curve", lambda *a, **k: calls.append(1))
    client.post("/plan/preview", json={"duration_hours": 1, "within_hours": 12})
    assert calls == []  # served from the cache, no re-forecast
