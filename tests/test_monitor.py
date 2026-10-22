import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config, models
from app.db import upsert
from app.ingest.pipeline import run_once
from app.main import app
from app.monitor import incidents as incidents_mod
from app.monitor.checks import Finding, check_data, check_ingest, check_jobs, check_models, robust_z
from app.monitor.drift import check_drift, daily_errors
from app.monitor.incidents import run_monitor, sync
from tests.fixtures import AWATTAR_MARKETDATA, ENERGY_CHARTS_POWER, ENERGY_CHARTS_PRICE, OPEN_METEO_PREVIOUS_RUNS
from tests.synthetic import seed_db

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def client(energy_charts_up: bool) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.energy-charts.info":
            if not energy_charts_up:
                return httpx.Response(503, text="No server is available to handle this request.")
            return httpx.Response(200, json=ENERGY_CHARTS_PRICE if request.url.path == "/price" else ENERGY_CHARTS_POWER)
        if request.url.host == "api.awattar.de":
            return httpx.Response(200, json=AWATTAR_MARKETDATA)
        return httpx.Response(200, json=OPEN_METEO_PREVIOUS_RUNS)

    return httpx.Client(transport=httpx.MockTransport(handler))


def keys(session) -> set[str]:
    """Open incidents about the data feeds (the tiny fixture data also trips unrelated data checks)."""
    return {
        i.key
        for i in session.scalars(select(models.Incident).where(models.Incident.status == "open"))
        if i.key.startswith(("ingest:", "fallback:"))
    }


def test_energy_charts_outage_drill(session, monkeypatch):
    """The autumn 2026 outage, replayed: Energy-Charts down for three hourly runs, then back."""
    monkeypatch.setattr("app.ingest.http.time.sleep", lambda _: None)
    now = datetime.now(timezone.utc)
    run_once(session, client(energy_charts_up=True), today=date(2026, 10, 20))
    run_monitor(session, now)
    assert keys(session) == set()

    for _ in range(3):
        run_once(session, client(energy_charts_up=False), today=date(2026, 10, 20))
    run_monitor(session, now)

    assert {"fallback:prices", "ingest:generation"} <= keys(session)
    fallback = session.scalars(select(models.Incident).where(models.Incident.key == "fallback:prices")).one()
    assert "aWATTar" in fallback.title
    assert "503" in fallback.note  # the real error is quoted as evidence
    assert "What GridPilot did" in fallback.note

    run_once(session, client(energy_charts_up=True), today=date(2026, 10, 20))
    result = run_monitor(session, now + timedelta(minutes=15))
    assert {i.key for i in result["resolved"]} >= {"fallback:prices", "ingest:generation"}
    assert keys(session) == set()
    assert "**Resolved:**" in fallback.note


def _run(session, dataset, status, when, source="energy_charts", error=None):
    session.add(models.IngestRun(started_at=when, finished_at=when, dataset=dataset, source=source, rows=0, status=status, error=error))
    session.commit()


def test_stale_feed_is_critical(session):
    _run(session, "weather", "ok", NOW - timedelta(hours=5), "open_meteo:north_sea")
    _run(session, "weather", "failed", NOW - timedelta(hours=1), "open_meteo:north_sea", "timeout")
    findings = check_ingest(session, NOW)
    assert [(f.key, f.severity) for f in findings] == [("ingest:weather", "critical")]


def test_stopped_ingestor_is_a_warning(session):
    _run(session, "prices", "ok", NOW - timedelta(hours=8))
    findings = check_ingest(session, NOW)
    assert [(f.key, f.kind, f.severity) for f in findings] == [("ingest:prices", "ingest_stopped", "warning")]
    assert "is the ingestor running" in findings[0].title


def test_one_failure_after_recent_success_is_not_an_incident(session):
    _run(session, "prices", "ok", NOW - timedelta(minutes=70))
    _run(session, "prices", "failed", NOW - timedelta(minutes=10), error="timeout")
    assert check_ingest(session, NOW) == []


def test_missing_prices_ahead_and_implausible_values(session):
    upsert(session, models.Price, [{"ts": NOW - timedelta(hours=2), "source": "energy_charts", "eur_mwh": 9999.0}], ["ts", "source"])
    session.commit()
    found = {f.key for f in check_data(session, NOW)}
    assert found == {"data:prices_ahead", "data:implausible"}


def _job(session, **kw):
    defaults = dict(
        name="j", task="demo", duration_hours=1, power_kw=1.0, earliest_start=NOW - timedelta(days=2), deadline=NOW + timedelta(hours=5),
        objective="cost", carbon_weight=0.5, status="planned", created_at=NOW - timedelta(days=2), replan_count=0,
        planned_start=NOW - timedelta(hours=1),
    )
    defaults.update(kw)
    job = models.Job(**defaults)
    session.add(job)
    session.commit()
    return job


def test_overdue_failed_and_missed_jobs(session):
    _job(session)  # planned an hour ago, never started
    _job(session, status="failed", error="RuntimeError: disk full", planned_start=NOW - timedelta(hours=3))
    _job(session, status="missed", planned_start=NOW - timedelta(hours=4))
    found = {f.key: f for f in check_jobs(session, NOW)}
    assert set(found) == {"jobs:overdue", "jobs:failed", "jobs:missed"}
    assert found["jobs:overdue"].severity == "critical"
    assert "disk full" in found["jobs:failed"].evidence[0]


def test_unusually_slow_task(session):
    for i in range(6):
        start = NOW - timedelta(days=3, hours=i)
        _job(session, task="ingest", status="succeeded", actual_start=start, actual_end=start + timedelta(seconds=20 + i), planned_start=start)
    slow_start = NOW - timedelta(hours=2)
    _job(session, task="ingest", status="succeeded", actual_start=slow_start, actual_end=slow_start + timedelta(seconds=400), planned_start=slow_start)
    found = [f for f in check_jobs(session, NOW) if f.key == "jobs:slow:ingest"]
    assert len(found) == 1 and "400 s" in found[0].summary


def test_robust_z_ignores_one_outlier_in_history():
    history = [10, 11, 9, 10, 12, 10, 500]  # one wild value in the past
    assert robust_z(30, history) > 4  # 30 is still clearly unusual


def test_model_checks(session, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FORECAST_MODEL", "lgbm")
    assert {f.key for f in check_models(session, NOW)} == {"model:missing:price", "model:missing:carbon"}

    models_dir = tmp_path / "models"
    monkeypatch.setattr(config, "MODEL_DIR", str(models_dir))
    models_dir.mkdir()
    for metric in ("price", "carbon"):
        (models_dir / f"lgbm_{metric}.txt").write_text("tree\n")
        (models_dir / f"lgbm_{metric}.json").write_text(
            json.dumps({"metric": metric, "trained_at": (NOW - timedelta(days=20)).isoformat(), "trained_through": "2026-09-30", "features": []})
        )
    monkeypatch.setattr("app.forecast.live.load_model", lambda m: (object(), json.loads((models_dir / f"lgbm_{m}.json").read_text())))
    assert {f.key for f in check_models(session, NOW)} == {"model:stale:price", "model:stale:carbon"}


def _store_forecasts(session, days: int, error_by_day):
    """Day-ahead forecasts for `days` days before NOW, off from the truth by error_by_day(i)."""
    seed_db(session, (NOW - timedelta(days=days + 40)).date(), NOW.date())
    from app.queries import hourly_prices

    actual = hourly_prices(session, NOW - timedelta(days=days + 1), NOW - timedelta(days=1))
    rows = []
    for row in actual.itertuples():
        day_index = (NOW - row.ts).days
        made_at = (row.ts - timedelta(hours=row.ts.hour + 14)).to_pydatetime()  # 10:00 the day before, roughly
        rows.append({"made_at": made_at, "target_ts": row.ts.to_pydatetime(), "metric": "price", "model": "profile_4w", "value": row.eur_mwh + error_by_day(day_index)})
    upsert(session, models.Forecast, rows, ["made_at", "target_ts", "metric", "model"])
    session.commit()


def test_drift_fires_when_recent_error_jumps(session, monkeypatch):
    monkeypatch.setattr(config, "FORECAST_MODEL", "profile_4w")
    _store_forecasts(session, 25, lambda d: 40.0 if d <= 4 else 5.0 + (d % 3))
    errors = daily_errors(session, "price", "profile_4w", NOW)
    assert len(errors) >= 17
    found = check_drift(session, NOW)
    assert [f.key for f in found] == ["drift:price"]
    assert "usual error" in found[0].title


def test_no_drift_when_error_is_steady(session, monkeypatch):
    monkeypatch.setattr(config, "FORECAST_MODEL", "profile_4w")
    _store_forecasts(session, 25, lambda d: 5.0 + (d % 3))
    assert check_drift(session, NOW) == []


def test_no_verdict_without_enough_history(session, monkeypatch):
    monkeypatch.setattr(config, "FORECAST_MODEL", "profile_4w")
    _store_forecasts(session, 8, lambda d: 50.0)
    assert check_drift(session, NOW) == []


def test_incident_lifecycle_and_webhook(session, monkeypatch):
    sent = []
    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
    monkeypatch.setattr(incidents_mod.httpx, "post", lambda url, json, timeout: sent.append(json) or httpx.Response(204, request=httpx.Request("POST", url)))

    finding = Finding(key="x:1", kind="test", severity="warning", title="Something broke", summary="details", evidence=["e1"])
    sync(session, [finding], NOW)
    sync(session, [finding], NOW + timedelta(minutes=15))
    incident = session.scalars(select(models.Incident)).one()
    assert incident.status == "open" and incident.checks == 2
    assert "- e1" in incident.note

    sync(session, [], NOW + timedelta(minutes=30))
    assert incident.status == "resolved" and "after 0.5 h" in incident.note
    assert len(sent) == 2  # one alert when opened, one when resolved
    assert sent[0]["content"].startswith("🔴") and "Something broke" in sent[0]["content"]
    assert sent[1]["content"].startswith("✅")


def test_metrics_and_incident_endpoints(session):
    sync(session, [Finding(key="x:1", kind="test", severity="critical", title="Broken", summary="s")], datetime.now(timezone.utc))
    with TestClient(app) as c:
        metrics = c.get("/metrics").text
        assert 'gridpilot_incidents_open{severity="critical"} 1.0' in metrics
        assert "gridpilot_jobs{" in metrics
        listed = c.get("/incidents", params={"status": "open"}).json()
        assert listed[0]["title"] == "Broken"
        assert c.get(f"/incidents/{listed[0]['id']}").json()["note"].startswith("## Broken")
        assert c.get(f"/incidents/{listed[0]['id']}/note").headers["content-type"].startswith("text/markdown")
