from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from tests.synthetic import seed_db


@pytest.fixture
def client(session):
    today = datetime.now(timezone.utc).date()
    seed_db(session, today - timedelta(days=40), today + timedelta(days=1))
    with TestClient(app) as c:
        yield c


def deadline_in(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def test_submit_explain_cancel(client):
    response = client.post(
        "/jobs",
        json={"name": "nightly", "task": "demo", "duration_hours": 2, "deadline": deadline_in(20), "objective": "cost"},
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "planned"
    assert job["planned_start"] is not None

    explain = client.get(f"/jobs/{job['id']}/explain").json()
    assert "possible start times were compared" in explain["summary"]
    assert explain["chosen"]["cost_eur"] <= explain["run_asap"]["cost_eur"]
    assert len(explain["curve"]) >= 2

    assert client.delete(f"/jobs/{job['id']}").json()["status"] == "cancelled"
    assert client.delete(f"/jobs/{job['id']}").status_code == 409  # can't cancel twice


def test_naive_times_are_read_as_berlin(client):
    local = (datetime.now(timezone.utc) + timedelta(hours=20)).astimezone().replace(tzinfo=None)
    response = client.post("/jobs", json={"name": "n", "task": "demo", "duration_hours": 1, "deadline": local.isoformat()})
    assert response.status_code == 201
    assert response.json()["deadline"].endswith("+00:00")


def test_unknown_task_is_422(client):
    response = client.post("/jobs", json={"name": "x", "task": "shutdown", "duration_hours": 1, "deadline": deadline_in(10)})
    assert response.status_code == 422
    assert "unknown task" in response.json()["detail"]


def test_list_and_filter(client):
    client.post("/jobs", json={"name": "a", "task": "demo", "duration_hours": 1, "deadline": deadline_in(10)})
    assert len(client.get("/jobs").json()) == 1
    assert client.get("/jobs", params={"status": "succeeded"}).json() == []
    assert client.get("/jobs/999").status_code == 404
    assert {t["task"] for t in client.get("/tasks").json()} == {"demo", "refresh_forecasts", "ingest"}


def test_api_key_enforced_when_set(client, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "s3cret")
    body = {"name": "a", "task": "demo", "duration_hours": 1, "deadline": deadline_in(10)}
    assert client.post("/jobs", json=body).status_code == 401
    assert client.post("/jobs", json=body, headers={"X-API-Key": "s3cret"}).status_code == 201
    assert client.get("/jobs").status_code == 200  # reading stays open
