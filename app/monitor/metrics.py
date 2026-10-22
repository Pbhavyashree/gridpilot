"""Prometheus metrics, computed from the database on every scrape.

GridPilot runs as several processes (API, ingestor, scheduler). Instead of each one
keeping its own counters, the API answers /metrics by reading the shared database.
That way the numbers are always consistent and survive restarts.
"""

import json
from datetime import datetime, timedelta, timezone

from prometheus_client import CollectorRegistry
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import desc, func, select

from app import config, models
from app.db import SessionLocal
from app.monitor.checks import DATASET_LABELS, aware
from app.queries import hourly_carbon, hourly_prices


class GridPilotCollector:
    def collect(self):
        now = datetime.now(timezone.utc)
        with SessionLocal() as session:
            yield from self._ingest(session, now)
            yield from self._data(session, now)
            yield from self._jobs(session, now)
            yield from self._incidents(session)
            yield from self._models(session, now)

    # ---- data feeds ----
    def _ingest(self, session, now):
        age = GaugeMetricFamily(
            "gridpilot_ingest_last_success_age_seconds", "Seconds since the last successful ingest", labels=["dataset"]
        )
        failed = GaugeMetricFamily("gridpilot_ingest_failed_runs_24h", "Failed ingest runs in the last 24 h", labels=["dataset"])
        for dataset in DATASET_LABELS:
            last_ok = session.scalar(
                select(models.IngestRun.finished_at)
                .where(models.IngestRun.dataset == dataset, models.IngestRun.status.in_(["ok", "fallback"]))
                .order_by(desc(models.IngestRun.id))
                .limit(1)
            )
            if last_ok is not None:
                age.add_metric([dataset], (now - aware(last_ok)).total_seconds())
            n_failed = session.scalar(
                select(func.count()).where(
                    models.IngestRun.dataset == dataset,
                    models.IngestRun.status == "failed",
                    models.IngestRun.started_at >= now - timedelta(hours=24),
                )
            )
            failed.add_metric([dataset], n_failed or 0)
        yield age
        yield failed

        latest = session.scalar(
            select(models.IngestRun.status).where(models.IngestRun.dataset == "prices").order_by(desc(models.IngestRun.id)).limit(1)
        )
        yield GaugeMetricFamily(
            "gridpilot_price_fallback_active", "1 if prices currently come from the aWATTar fallback", value=1 if latest == "fallback" else 0
        )

    # ---- data itself ----
    def _data(self, session, now):
        latest_price = aware(session.scalar(select(func.max(models.Price.ts))))
        if latest_price:
            yield GaugeMetricFamily(
                "gridpilot_prices_published_ahead_hours",
                "Hours of published day-ahead prices still ahead of now",
                value=(latest_price + timedelta(hours=1) - now).total_seconds() / 3600,
            )
        latest_carbon = aware(session.scalar(select(func.max(models.CarbonIntensity.ts))))
        if latest_carbon:
            yield GaugeMetricFamily(
                "gridpilot_carbon_data_lag_hours", "Age of the newest measured carbon value", value=(now - latest_carbon).total_seconds() / 3600
            )

        hour = now.replace(minute=0, second=0, microsecond=0)
        price = hourly_prices(session, hour, hour + timedelta(hours=1))
        if not price.empty:
            yield GaugeMetricFamily("gridpilot_current_price_eur_mwh", "Day-ahead price for the current hour", value=float(price["eur_mwh"].iloc[0]))
        carbon = hourly_carbon(session, hour - timedelta(hours=3), hour + timedelta(hours=1))
        if not carbon.empty:
            yield GaugeMetricFamily(
                "gridpilot_latest_carbon_g_per_kwh", "Latest measured grid carbon intensity", value=float(carbon["g_per_kwh"].iloc[-1])
            )

        from app.monitor.drift import recent_mae

        mae = GaugeMetricFamily(
            "gridpilot_forecast_mae_7d", "Day-ahead forecast error over the last 7 days (needs stored forecasts)", labels=["metric"]
        )
        for metric in ("price", "carbon"):
            value = recent_mae(session, metric, now)
            if value is not None:
                mae.add_metric([metric], value)
        yield mae

    # ---- jobs ----
    def _jobs(self, session, now):
        jobs = GaugeMetricFamily("gridpilot_jobs", "Jobs by status", labels=["status"])
        counts = dict(session.execute(select(models.Job.status, func.count()).group_by(models.Job.status)).all())
        for status in ("planned", "running", "succeeded", "failed", "missed", "cancelled"):
            jobs.add_metric([status], counts.get(status, 0))
        yield jobs

        overdue = session.scalar(
            select(func.count()).where(models.Job.status == "planned", models.Job.planned_start < now - timedelta(minutes=10))
        )
        yield GaugeMetricFamily("gridpilot_jobs_overdue", "Planned jobs more than 10 min past their start", value=overdue or 0)

        saved_eur = saved_kg = 0.0
        for plan_json in session.scalars(select(models.Job.plan_json).where(models.Job.status.in_(["planned", "running", "succeeded"]))):
            if plan_json:
                savings = json.loads(plan_json).get("savings_vs_asap", {})
                saved_eur += savings.get("cost_eur") or 0.0
                saved_kg += savings.get("co2_kg") or 0.0
        yield GaugeMetricFamily("gridpilot_expected_savings_eur", "Expected cost saved vs running jobs immediately", value=saved_eur)
        yield GaugeMetricFamily("gridpilot_expected_savings_co2_kg", "Expected CO2 avoided vs running jobs immediately", value=saved_kg)

    # ---- incidents ----
    def _incidents(self, session):
        family = GaugeMetricFamily("gridpilot_incidents_open", "Open incidents", labels=["severity"])
        counts = dict(
            session.execute(
                select(models.Incident.severity, func.count()).where(models.Incident.status == "open").group_by(models.Incident.severity)
            ).all()
        )
        for severity in ("warning", "critical"):
            family.add_metric([severity], counts.get(severity, 0))
        yield family

    # ---- models ----
    def _models(self, session, now):
        if config.FORECAST_MODEL != "lgbm":
            return
        from app.forecast.live import load_model

        family = GaugeMetricFamily("gridpilot_model_age_hours", "Hours since the lgbm model was trained", labels=["metric"])
        for metric in ("price", "carbon"):
            _, meta = load_model(metric)
            if meta:
                family.add_metric([metric], (now - datetime.fromisoformat(meta["trained_at"])).total_seconds() / 3600)
        yield family


registry = CollectorRegistry()
registry.register(GridPilotCollector())
