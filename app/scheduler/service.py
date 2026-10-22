"""Job lifecycle: create, plan, replan, run, settle.

Every function takes `now` as a parameter instead of reading the clock itself.
That keeps the logic testable: a test can say "it is now 03:00" without waiting.
"""

import json
import logging
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select

from app import config, models
from app.queries import hourly_carbon, hourly_prices
from app.scheduler.curve import build_curve
from app.scheduler.placement import InfeasibleJob, Placement, ceil_hour, place
from app.scheduler.tasks import TASKS

log = logging.getLogger(__name__)


class JobRejected(ValueError):
    """The job request itself is invalid (unknown task, deadline too far away, ...)."""


# ---------- planning ----------


def _snapshot(placement: Placement, curve: pd.DataFrame, now: datetime) -> str:
    """Everything /explain needs, frozen at planning time."""

    def window(w):
        return {"start": w.start.isoformat(), "end": w.end.isoformat(), "cost_eur": w.cost_eur, "co2_kg": w.co2_kg}

    points = [
        {
            "ts": row.ts.isoformat(),
            "eur_mwh": None if pd.isna(row.eur_mwh) else round(float(row.eur_mwh), 2),
            "price_kind": row.price_kind,
            "g_per_kwh": None if pd.isna(row.g_per_kwh) else round(float(row.g_per_kwh), 1),
            "carbon_kind": row.carbon_kind,
        }
        for row in curve.itertuples()
    ]
    return json.dumps(
        {
            "planned_at": now.isoformat(),
            "forecast_model": config.FORECAST_MODEL,
            "objective": placement.objective,
            "carbon_weight": placement.carbon_weight,
            "candidates": placement.candidates,
            "chosen": window(placement.chosen),
            "run_asap": window(placement.asap),
            "savings_vs_asap": placement.savings,
            "curve": points,
        }
    )


def plan_job(session, job: models.Job, now: datetime) -> Placement:
    earliest = max(job.earliest_start, now)
    curve = build_curve(session, ceil_hour(earliest), job.deadline)
    placement = place(
        curve,
        job.duration_hours,
        earliest,
        job.deadline,
        objective=job.objective,
        carbon_weight=job.carbon_weight,
        power_kw=job.power_kw,
    )
    job.planned_start = placement.chosen.start
    job.planned_at = now
    job.expected_cost_eur = placement.chosen.cost_eur
    job.expected_co2_kg = placement.chosen.co2_kg
    job.plan_json = _snapshot(placement, curve, now)
    return placement


def create_job(session, data: dict, now: datetime) -> models.Job:
    if data["task"] not in TASKS:
        raise JobRejected(f"unknown task '{data['task']}'; allowed: {', '.join(TASKS)}")
    earliest = max(data.get("earliest_start") or now, now)
    deadline = data["deadline"]
    if deadline > now + timedelta(hours=config.MAX_HORIZON_HOURS):
        raise JobRejected(f"deadline is more than {config.MAX_HORIZON_HOURS}h away; forecasts aren't useful that far out")
    if not 1 <= data["duration_hours"] <= config.MAX_DURATION_HOURS:
        raise JobRejected(f"duration_hours must be between 1 and {config.MAX_DURATION_HOURS}")

    job = models.Job(
        name=data["name"],
        task=data["task"],
        duration_hours=data["duration_hours"],
        power_kw=data.get("power_kw", 1.0),
        earliest_start=earliest,
        deadline=deadline,
        objective=data.get("objective", "cost"),
        carbon_weight=data.get("carbon_weight", 0.5),
        status="planned",
        created_at=now,
        replan_count=0,
    )
    try:
        plan_job(session, job, now)
    except InfeasibleJob as exc:
        raise JobRejected(str(exc)) from exc
    session.add(job)
    session.commit()
    session.refresh(job)
    log.info("Job %s '%s' planned for %s", job.id, job.name, job.planned_start)
    return job


def replan_pending(session, now: datetime) -> int:
    """Re-place planned jobs: forecasts improve and real prices get published over time.

    Jobs starting within the freeze window are left alone, so nothing gets yanked
    around minutes before it runs. Returns how many jobs moved.
    """
    freeze_until = now + timedelta(minutes=config.REPLAN_FREEZE_MINUTES)
    jobs = session.scalars(
        select(models.Job).where(models.Job.status == "planned", models.Job.planned_start > freeze_until)
    ).all()
    moved = 0
    for job in jobs:
        before = job.planned_start
        try:
            plan_job(session, job, now)
        except InfeasibleJob as exc:
            log.warning("Replan of job %s failed, keeping the old plan: %s", job.id, exc)
            continue
        if job.planned_start != before:
            job.replan_count += 1
            moved += 1
            log.info("Job %s moved from %s to %s", job.id, before, job.planned_start)
    session.commit()
    return moved


# ---------- running ----------


def run_due(session, now: datetime) -> list[int]:
    """Start every planned job whose start time has come. Returns the ids that ran."""
    due = session.scalars(
        select(models.Job).where(models.Job.status == "planned", models.Job.planned_start <= now).order_by(models.Job.planned_start)
    ).all()
    ran = []
    for job in due:
        if now + timedelta(hours=job.duration_hours) > job.deadline:
            job.status = "missed"
            job.error = f"worker reached it at {now.isoformat()}, too late to finish by the deadline"
            session.commit()
            log.error("Job %s missed its deadline", job.id)
            continue

        job.status = "running"
        job.actual_start = now
        session.commit()
        try:
            message = TASKS[job.task].run(session)
            job.status = "succeeded"
            job.error = None
            log.info("Job %s succeeded: %s", job.id, message)
        except Exception as exc:  # noqa: BLE001  (any task failure is recorded, never crashes the worker)
            session.rollback()
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            log.exception("Job %s failed", job.id)
        job.actual_end = datetime.now(now.tzinfo)
        session.commit()
        ran.append(job.id)
    return ran


def settle(session, now: datetime) -> int:
    """Fill in realised cost and CO2 once a job's window is over and real data exists for all of it."""
    jobs = session.scalars(
        select(models.Job).where(
            models.Job.status.in_(["succeeded", "failed"]), models.Job.realised_cost_eur.is_(None)
        )
    ).all()
    settled = 0
    for job in jobs:
        start = job.actual_start.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=job.duration_hours)
        if end > now:
            continue
        prices = hourly_prices(session, start, end)
        carbon = hourly_carbon(session, start, end)
        if len(prices) < job.duration_hours or len(carbon) < job.duration_hours:
            continue  # measured data not in yet; try again next hour
        job.realised_cost_eur = round(float(prices["eur_mwh"].sum()) * job.power_kw / 1000, 4)
        job.realised_co2_kg = round(float(carbon["g_per_kwh"].sum()) * job.power_kw / 1000, 4)
        settled += 1
    session.commit()
    return settled
