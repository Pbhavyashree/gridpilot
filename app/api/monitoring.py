"""Monitoring endpoints: Prometheus metrics and incidents."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import desc, select

from app import models
from app.api.jobs import get_session

router = APIRouter(tags=["monitoring"])


def incident_out(i: models.Incident, with_note: bool = False) -> dict:
    out = {
        "id": i.id,
        "key": i.key,
        "kind": i.kind,
        "severity": i.severity,
        "status": i.status,
        "title": i.title,
        "first_seen": i.first_seen.isoformat(),
        "last_seen": i.last_seen.isoformat(),
        "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
        "checks": i.checks,
    }
    if with_note:
        out["note"] = i.note
    return out


@router.get("/metrics", include_in_schema=False)
def metrics():
    from app.monitor.metrics import registry

    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


@router.get("/incidents")
def list_incidents(
    status: Literal["open", "resolved"] | None = None,
    limit: int = Query(50, ge=1, le=500),
    session=Depends(get_session),
):
    """What GridPilot noticed about itself, newest first."""
    query = select(models.Incident).order_by(desc(models.Incident.id)).limit(limit)
    if status:
        query = query.where(models.Incident.status == status)
    return [incident_out(i) for i in session.scalars(query)]


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: int, session=Depends(get_session)):
    """One incident with its full write-up (Markdown in `note`)."""
    incident = session.get(models.Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id} not found")
    return incident_out(incident, with_note=True)


@router.get("/incidents/{incident_id}/note", response_class=Response)
def incident_note(incident_id: int, session=Depends(get_session)):
    """Just the write-up, as plain Markdown, handy for pasting into a ticket."""
    incident = session.get(models.Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id} not found")
    return Response(incident.note, media_type="text/markdown; charset=utf-8")
