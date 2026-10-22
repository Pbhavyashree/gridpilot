from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_ok():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_prices_empty_database_returns_empty_list():
    with TestClient(app) as client:
        response = client.get("/prices")
    assert response.status_code == 200
    assert response.json() == []


def test_ingest_runs_endpoint():
    with TestClient(app) as client:
        response = client.get("/ingest/runs")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
