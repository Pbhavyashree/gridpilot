from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import models
from app.db import upsert
from app.scheduler import service
from app.scheduler.curve import build_curve
from app.scheduler.service import JobRejected, create_job, replan_pending, run_due, settle
from app.scheduler.tasks import TASKS, Task
from tests.synthetic import seed_db

# "Now" for these tests: 20 Oct 2026, 10:00 UTC. Data is loaded through the end of 20 Oct (Berlin).
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded(session):
    seed_db(session, date(2026, 9, 1), date(2026, 10, 20))
    return session


def job_data(**overrides):
    data = {
        "name": "test job",
        "task": "demo",
        "duration_hours": 2,
        "deadline": NOW + timedelta(hours=24),
        "objective": "cost",
    }
    data.update(overrides)
    return data


def test_curve_uses_real_data_then_forecast(seeded):
    curve = build_curve(seeded, NOW, NOW + timedelta(hours=36))
    assert len(curve) == 36
    assert curve["price_kind"].iloc[0] == "published"
    assert curve["price_kind"].iloc[-1] == "forecast"
    assert set(curve["carbon_kind"]) <= {"measured", "forecast"}
    assert curve[["eur_mwh", "g_per_kwh"]].notna().all().all()


def test_create_job_plans_into_cheapest_window(seeded):
    job = create_job(seeded, job_data(), NOW)
    assert job.status == "planned"
    assert NOW <= job.planned_start <= job.deadline - timedelta(hours=2)
    assert job.expected_cost_eur is not None and job.plan_json


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"task": "rm -rf /"}, "unknown task"),
        ({"deadline": NOW + timedelta(hours=100)}, "more than"),
        ({"duration_hours": 5, "deadline": NOW + timedelta(hours=3)}, "can't fit"),
    ],
)
def test_bad_jobs_are_rejected(seeded, overrides, message):
    with pytest.raises(JobRejected, match=message):
        create_job(seeded, job_data(**overrides), NOW)


def test_replan_moves_job_when_cheaper_prices_appear(seeded):
    # Earliest start 2h out, so the plan isn't inside the "about to start" freeze window.
    job = create_job(
        seeded, job_data(duration_hours=1, earliest_start=NOW + timedelta(hours=2), deadline=NOW + timedelta(hours=12)), NOW
    )
    cheap_hour = NOW + timedelta(hours=9)
    assert job.planned_start != cheap_hour

    upsert(seeded, models.Price, [{"ts": cheap_hour, "source": "energy_charts", "eur_mwh": -500.0}], ["ts", "source"])
    seeded.commit()

    assert replan_pending(seeded, NOW + timedelta(minutes=5)) == 1
    seeded.refresh(job)
    assert job.planned_start == cheap_hour
    assert job.replan_count == 1


def test_replan_leaves_imminent_jobs_alone(seeded):
    job = create_job(seeded, job_data(duration_hours=1, deadline=NOW + timedelta(hours=2)), NOW)
    just_before = job.planned_start - timedelta(minutes=5)
    upsert(seeded, models.Price, [{"ts": NOW + timedelta(hours=1), "source": "energy_charts", "eur_mwh": -999.0}], ["ts", "source"])
    seeded.commit()
    replan_pending(seeded, just_before)
    seeded.refresh(job)
    assert job.replan_count == 0


def test_run_due_runs_and_settles(seeded):
    # Deadline inside the loaded data, so real values exist to settle against.
    job = create_job(seeded, job_data(deadline=NOW + timedelta(hours=10)), NOW)
    assert run_due(seeded, job.planned_start - timedelta(minutes=1)) == []  # not yet

    assert run_due(seeded, job.planned_start) == [job.id]
    seeded.refresh(job)
    assert job.status == "succeeded"
    assert job.actual_start == job.planned_start

    # Realised values arrive once the window is over and data exists for it.
    assert settle(seeded, job.planned_start + timedelta(hours=1)) == 0
    assert settle(seeded, job.planned_start + timedelta(hours=2)) == 1
    seeded.refresh(job)
    assert job.realised_cost_eur == pytest.approx(job.expected_cost_eur, rel=1e-6)  # synthetic data was "published"


def test_late_worker_marks_job_missed(seeded):
    job = create_job(seeded, job_data(duration_hours=2, deadline=NOW + timedelta(hours=4)), NOW)
    run_due(seeded, NOW + timedelta(hours=3))  # only 1h left before the deadline
    seeded.refresh(job)
    assert job.status == "missed"


def test_failing_task_is_recorded_not_raised(seeded, monkeypatch):
    def boom(session):
        raise RuntimeError("disk full")

    monkeypatch.setitem(TASKS, "explode", Task("always fails", boom))
    job = create_job(seeded, job_data(task="explode"), NOW)
    run_due(seeded, job.planned_start)
    seeded.refresh(job)
    assert job.status == "failed"
    assert "disk full" in job.error


def test_cancelled_jobs_never_run(seeded):
    job = create_job(seeded, job_data(), NOW)
    job.status = "cancelled"
    seeded.commit()
    assert run_due(seeded, job.deadline) == []
    assert seeded.scalars(select(models.Job.status)).one() == "cancelled"


def test_weekly_retrain_is_submitted_once(seeded):
    from app.scheduler.cli import submit_weekly_retrain

    first = submit_weekly_retrain(NOW)
    assert first is not None and first.task == "retrain_models" and first.objective == "carbon"
    assert submit_weekly_retrain(NOW) is None  # one already waiting: don't queue a second
    retrains = seeded.scalars(select(models.Job).where(models.Job.task == "retrain_models")).all()
    assert len(retrains) == 1
