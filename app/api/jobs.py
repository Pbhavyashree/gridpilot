"""Job endpoints: submit, list, inspect, explain, cancel."""

import json
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select

from app import config, models
from app.db import SessionLocal
from app.forecast.baselines import LOCAL_TZ
from app.scheduler.service import JobRejected, create_job
from app.scheduler.tasks import TASKS

router = APIRouter(tags=["jobs"])


def get_session():
    with SessionLocal() as session:
        yield session


def require_api_key(x_api_key: str | None = Header(default=None)):
    """Only enforced when API_KEY is set (it will be in production)."""
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="missing or wrong X-API-Key header")


class JobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["Nightly report refresh"])
    task: str = Field(examples=["demo"])
    duration_hours: int = Field(ge=1, examples=[2])
    power_kw: float = Field(default=1.0, gt=0, le=10_000, description="Average power draw while running")
    earliest_start: datetime | None = Field(default=None, description="Defaults to now")
    deadline: datetime = Field(description="Must finish by then. Times without an offset are read as Berlin time.")
    objective: Literal["cost", "carbon", "blend"] = "cost"
    carbon_weight: float = Field(default=0.5, ge=0, le=1, description="Only used with objective=blend")

    @field_validator("earliest_start", "deadline")
    @classmethod
    def assume_berlin_if_naive(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(LOCAL_TZ))
        return value.astimezone(timezone.utc)


def job_out(job: models.Job) -> dict:
    def iso(value):
        return value.isoformat() if value else None

    return {
        "id": job.id,
        "name": job.name,
        "task": job.task,
        "status": job.status,
        "objective": job.objective,
        "duration_hours": job.duration_hours,
        "power_kw": job.power_kw,
        "earliest_start": iso(job.earliest_start),
        "deadline": iso(job.deadline),
        "planned_start": iso(job.planned_start),
        "planned_at": iso(job.planned_at),
        "replan_count": job.replan_count,
        "expected": {"cost_eur": job.expected_cost_eur, "co2_kg": job.expected_co2_kg},
        "actual_start": iso(job.actual_start),
        "actual_end": iso(job.actual_end),
        "realised": {"cost_eur": job.realised_cost_eur, "co2_kg": job.realised_co2_kg},
        "error": job.error,
    }


def _get_job(session, job_id: int) -> models.Job:
    job = session.get(models.Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job


@router.get("/tasks")
def list_tasks():
    """The tasks a job is allowed to run."""
    return [{"task": name, "description": t.description} for name, t in TASKS.items()]


@router.post("/jobs", status_code=201, dependencies=[Depends(require_api_key)])
def submit_job(body: JobCreate, session=Depends(get_session)):
    """Submit a job; GridPilot picks when it runs and answers with the plan."""
    try:
        job = create_job(session, body.model_dump(), datetime.now(timezone.utc))
    except JobRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return job_out(job)


@router.get("/jobs")
def list_jobs(
    status: Literal["planned", "running", "succeeded", "failed", "missed", "cancelled"] | None = None,
    limit: int = Query(50, ge=1, le=500),
    session=Depends(get_session),
):
    query = select(models.Job).order_by(desc(models.Job.id)).limit(limit)
    if status:
        query = query.where(models.Job.status == status)
    return [job_out(j) for j in session.scalars(query)]


@router.get("/jobs/{job_id}")
def get_job(job_id: int, session=Depends(get_session)):
    return job_out(_get_job(session, job_id))


@router.get("/jobs/{job_id}/explain")
def explain_job(job_id: int, session=Depends(get_session)):
    """Why this start time? The curve the scheduler saw, what running immediately would have
    cost, and how much the chosen window saves."""
    job = _get_job(session, job_id)
    if not job.plan_json:
        raise HTTPException(status_code=404, detail="this job has no plan")
    plan = json.loads(job.plan_json)
    chosen, asap, savings = plan["chosen"], plan["run_asap"], plan["savings_vs_asap"]

    def pct(value):
        return "n/a" if value is None else f"{value:.0f}%"

    start_local = datetime.fromisoformat(chosen["start"]).astimezone(ZoneInfo(LOCAL_TZ))
    summary = (
        f"Optimising for {plan['objective']}, {plan['candidates']} possible start times were compared. "
        f"Starting at {start_local:%a %d %b %H:%M} (Berlin) is expected to cost {chosen['cost_eur']:.4f} EUR and emit {chosen['co2_kg']:.3f} kg CO2, "
        f"versus {asap['cost_eur']:.4f} EUR and {asap['co2_kg']:.3f} kg if it ran as soon as possible "
        f"({pct(savings['cost_pct'])} cheaper, {pct(savings['co2_pct'])} less CO2)."
    )
    kinds = {}
    for point in plan["curve"]:
        for key in ("price_kind", "carbon_kind"):
            kinds.setdefault(key, {}).setdefault(point[key], 0)
            kinds[key][point[key]] += 1

    return {"job": job_out(job), "summary": summary, "data_used": kinds, **plan}


@router.delete("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def cancel_job(job_id: int, session=Depends(get_session)):
    job = _get_job(session, job_id)
    if job.status != "planned":
        raise HTTPException(status_code=409, detail=f"only planned jobs can be cancelled (this one is {job.status})")
    job.status = "cancelled"
    session.commit()
    return job_out(job)
