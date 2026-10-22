"""The public website: the front page, plus the two read-only endpoints it runs on.

    GET  /               the page (app/static/index.html)
    GET  /overview       everything the page shows, in one request
    POST /plan/preview   "when should I run a job like this?", answered without saving anything

Both endpoints are public on purpose: they only read. Creating real jobs still needs
the API key (POST /jobs). The curve is cached for a few minutes, so a busy page can't
make the server re-forecast on every click.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from app import config, models
from app.api.jobs import get_session, job_out
from app.forecast.baselines import LOCAL_TZ
from app.monitor.checks import DATASET_LABELS, aware
from app.scheduler.curve import build_curve
from app.scheduler.placement import InfeasibleJob, ceil_hour, place

router = APIRouter()
STATIC = Path(__file__).resolve().parent.parent / "static"
BERLIN = ZoneInfo(LOCAL_TZ)
CURVE_HOURS = 48
CACHE_SECONDS = 300

_cache: dict = {}
_lock = threading.Lock()


def cached_curve(session, now: datetime) -> pd.DataFrame:
    """The next CURVE_HOURS hours, rebuilt at most every few minutes (and whenever the hour turns)."""
    start = now.replace(minute=0, second=0, microsecond=0)
    with _lock:
        hit = _cache.get("curve")
        if hit and hit["start"] == start and time.monotonic() - hit["built"] < CACHE_SECONDS:
            return hit["curve"]
        curve = build_curve(session, start, start + timedelta(hours=CURVE_HOURS))
        _cache["curve"] = {"start": start, "built": time.monotonic(), "curve": curve}
        return curve


def _points(curve: pd.DataFrame) -> list[dict]:
    def num(value, digits):
        return None if pd.isna(value) else round(float(value), digits)

    return [
        {
            "ts": row.ts.isoformat(),
            "eur_mwh": num(row.eur_mwh, 2),
            "price_kind": row.price_kind,
            "g_per_kwh": num(row.g_per_kwh, 1),
            "carbon_kind": row.carbon_kind,
        }
        for row in curve.itertuples()
    ]


@router.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@router.get("/overview", tags=["site"])
def overview(session=Depends(get_session)):
    """Everything the front page shows: the next 48 hours, system health, jobs and models."""
    now = datetime.now(timezone.utc)
    curve = cached_curve(session, now)

    # Data freshness per feed
    feeds = []
    for dataset, label in DATASET_LABELS.items():
        last_ok = session.scalar(
            select(models.IngestRun.finished_at)
            .where(models.IngestRun.dataset == dataset, models.IngestRun.status.in_(["ok", "fallback"]))
            .order_by(desc(models.IngestRun.id))
            .limit(1)
        )
        feeds.append({"dataset": dataset, "label": label, "last_success": aware(last_ok).isoformat() if last_ok else None})

    open_incidents = session.scalars(
        select(models.Incident).where(models.Incident.status == "open").order_by(desc(models.Incident.id))
    ).all()
    resolved = session.scalars(
        select(models.Incident).where(models.Incident.status == "resolved").order_by(desc(models.Incident.id)).limit(5)
    ).all()

    def incident(i):
        return {
            "id": i.id,
            "severity": i.severity,
            "title": i.title,
            "first_seen": i.first_seen.isoformat(),
            "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
        }

    from app.forecast.live import model_info

    jobs = session.scalars(select(models.Job).order_by(desc(models.Job.id)).limit(20)).all()
    saved_eur = saved_kg = 0.0
    for plan_json in session.scalars(select(models.Job.plan_json).where(models.Job.status.in_(["planned", "running", "succeeded"]))):
        if plan_json:
            s = json.loads(plan_json).get("savings_vs_asap", {})
            saved_eur += s.get("cost_eur") or 0.0
            saved_kg += s.get("co2_kg") or 0.0
    counts = dict(session.execute(select(models.Job.status, func.count()).group_by(models.Job.status)).all())

    return {
        "generated_at": now.isoformat(),
        "curve": _points(curve),
        "forecast_models": curve.attrs.get("models_used", {}),
        "health": {
            "open": [incident(i) for i in open_incidents],
            "recently_resolved": [incident(i) for i in resolved],
            "feeds": feeds,
        },
        "models": model_info(),
        "jobs": [job_out(j) for j in jobs],
        "job_counts": counts,
        "expected_savings": {"cost_eur": round(saved_eur, 4), "co2_kg": round(saved_kg, 4)},
    }


class PreviewRequest(BaseModel):
    duration_hours: int = Field(ge=1, examples=[2])
    within_hours: int = Field(ge=1, le=CURVE_HOURS, examples=[24], description="Must finish within this many hours from now")
    objective: Literal["cost", "carbon", "blend"] = "carbon"
    carbon_weight: float = Field(default=0.5, ge=0, le=1)
    power_kw: float = Field(default=1.0, gt=0, le=10_000)


@router.post("/plan/preview", tags=["site"])
def plan_preview(body: PreviewRequest, session=Depends(get_session)):
    """When would GridPilot run a job like this? Nothing is saved, so no API key is needed."""
    if body.duration_hours > min(config.MAX_DURATION_HOURS, body.within_hours):
        raise HTTPException(status_code=422, detail="the job is longer than the time you gave it to finish")
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=body.within_hours)
    curve = cached_curve(session, now)
    try:
        p = place(curve, body.duration_hours, now, deadline, body.objective, body.carbon_weight, body.power_kw)
    except InfeasibleJob as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def window(w):
        return {
            "start": w.start.isoformat(),
            "end": w.end.isoformat(),
            "start_local": w.start.astimezone(BERLIN).strftime("%a %H:%M"),
            "cost_eur": w.cost_eur,
            "co2_kg": w.co2_kg,
        }

    return {
        "chosen": window(p.chosen),
        "run_now": window(p.asap),
        "savings": p.savings,
        "starts_now": p.chosen.start <= ceil_hour(now),
        "candidates": p.candidates,
        "deadline": deadline.isoformat(),
        "forecast_models": curve.attrs.get("models_used", {}),
    }
