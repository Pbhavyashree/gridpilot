"""FastAPI application. Run locally with:  uvicorn app.main:app --reload"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import desc, select, text

from app import models
from app.db import SessionLocal, init_db
from app.forecast.baselines import METRIC_UNITS
from app.forecast.service import latest_forecast
from app.queries import hourly_carbon, hourly_prices


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="GridPilot",
    description="Energy-aware job scheduler for the German grid. Data: Energy-Charts / Fraunhofer ISE, aWATTar.",
    version="0.1.0",
    lifespan=lifespan,
)


def get_session():
    with SessionLocal() as session:
        yield session


@app.get("/health")
def health(session=Depends(get_session)):
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return {"status": "ok"}


@app.get("/prices")
def prices(hours_back: int = Query(24, ge=1, le=24 * 31), hours_ahead: int = Query(36, ge=0, le=72), session=Depends(get_session)):
    """Hourly day-ahead price and estimated carbon intensity around now."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start, end = now - timedelta(hours=hours_back), now + timedelta(hours=hours_ahead)

    price_df = hourly_prices(session, start, end)
    carbon_df = hourly_carbon(session, start, end)
    merged = price_df.merge(carbon_df, on="ts", how="outer").sort_values("ts")

    return [
        {
            "ts": row.ts.isoformat(),
            "eur_mwh": None if row.eur_mwh != row.eur_mwh else round(row.eur_mwh, 2),  # NaN check
            "g_co2_per_kwh": None if row.g_per_kwh != row.g_per_kwh else round(row.g_per_kwh, 1),
            "price_source": row.source if isinstance(row.source, str) else None,
        }
        for row in merged.itertuples()
    ]


@app.get("/forecast")
def forecast(
    metric: Literal["price", "carbon"] = "price",
    model: Literal["naive_day", "naive_week", "profile_4w"] = "profile_4w",
    session=Depends(get_session),
):
    """The most recent stored forecast. Create one with: python -m app.forecast.cli forecast"""
    rows = latest_forecast(session, metric, model)
    if not rows:
        raise HTTPException(status_code=404, detail="no forecast yet; run: python -m app.forecast.cli forecast")
    return {
        "metric": metric,
        "model": model,
        "unit": METRIC_UNITS[metric],
        "made_at": rows[0].made_at.isoformat(),
        "values": [{"ts": r.target_ts.isoformat(), "value": round(r.value, 2)} for r in rows],
    }


@app.get("/ingest/runs")
def ingest_runs(limit: int = Query(20, ge=1, le=200), session=Depends(get_session)):
    """Latest ingest attempts, newest first. Useful for spotting outages."""
    runs = session.scalars(select(models.IngestRun).order_by(desc(models.IngestRun.id)).limit(limit)).all()
    return [
        {
            "started_at": r.started_at.isoformat(),
            "dataset": r.dataset,
            "source": r.source,
            "status": r.status,
            "rows": r.rows,
            "error": r.error,
        }
        for r in runs
    ]
