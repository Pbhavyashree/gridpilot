from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.forecast import live, ml
from app.forecast.live import forecast_values, load_model, train_and_save
from app.main import app
from app.scheduler.curve import build_curve
from app.scheduler.service import create_job
from tests.synthetic import seed_db

TODAY = date(2026, 10, 20)


@pytest.fixture
def history(session, monkeypatch):
    monkeypatch.setattr(ml, "MIN_TRAIN_DAYS", 30)  # small synthetic history is enough for a test
    monkeypatch.setattr(ml, "NUM_BOOST_ROUND", 50)
    seed_db(session, TODAY - timedelta(days=90), TODAY, noise=5.0)
    return session


def test_without_a_trained_model_lgbm_falls_back(history):
    values, used = forecast_values(history, "price", "lgbm", TODAY + timedelta(days=1))
    assert used == "profile_4w (fallback: no trained lgbm)"
    assert not np.isnan(values).any()


def test_train_save_load_predict(history):
    meta = train_and_save(history, "price", today=TODAY)
    assert meta["trained_through"] == TODAY.isoformat()  # prices for today are published
    assert meta["train_days"] >= 30
    assert meta["uses_weather"] is False

    booster, loaded = load_model("price")
    assert booster is not None and loaded["features"] == meta["features"]

    values, used = forecast_values(history, "price", "lgbm", TODAY + timedelta(days=1))
    assert used == "lgbm"
    assert values.shape == (24,) and np.isfinite(values).all()


def test_carbon_model_trains_only_on_complete_days(history):
    meta = train_and_save(history, "carbon", today=TODAY)
    assert meta["trained_through"] == (TODAY - timedelta(days=1)).isoformat()


def test_not_enough_history_is_a_clear_error(session, monkeypatch):
    seed_db(session, TODAY - timedelta(days=50), TODAY)
    with pytest.raises(ValueError, match="need"):
        train_and_save(session, "price", today=TODAY)


def test_scheduler_records_which_model_it_used(history):
    now = datetime.combine(TODAY, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=10)
    start, end = now, now + timedelta(hours=40)

    assert "fallback" in build_curve(history, start, end).attrs["models_used"]["carbon"]

    train_and_save(history, "price", today=TODAY)
    train_and_save(history, "carbon", today=TODAY)
    curve = build_curve(history, start, end)
    assert curve.attrs["models_used"] == {"price": "lgbm", "carbon": "lgbm"}

    job = create_job(history, {"name": "j", "task": "demo", "duration_hours": 2, "deadline": end}, now)
    assert '"forecast_models_used"' in job.plan_json


def test_models_endpoint(history):
    with TestClient(app) as client:
        before = client.get("/models").json()
        assert all(m["trained_at"] is None for m in before)
        train_and_save(history, "price", today=TODAY)
        after = {m["metric"]: m for m in client.get("/models").json()}
    assert after["price"]["trained_at"] is not None
    assert after["carbon"]["trained_at"] is None


def test_model_reloads_after_retraining(history):
    train_and_save(history, "price", today=TODAY)
    first, _ = load_model("price")
    train_and_save(history, "price", today=TODAY)
    second, _ = load_model("price")
    assert first is not second
    live._cache.clear()
