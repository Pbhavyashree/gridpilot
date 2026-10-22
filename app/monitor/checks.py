"""Health checks: GridPilot looking at itself.

Each check returns Findings. A finding isn't an incident yet; incidents.sync() turns
findings into incidents (opening new ones, updating ongoing ones, resolving the ones
that stopped showing up).

Every check takes `now` as a parameter, so tests can say "it is now 15:00" and
simulate an outage without waiting for one.

Rule-based checks catch most real problems in a system like this. The statistical
ones (forecast drift, unusually slow jobs) use robust z-scores (median and MAD
instead of mean and standard deviation), so a single wild value can't hide or fake
an anomaly.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import desc, func, select

from app import config, models


@dataclass
class Finding:
    key: str  # stable identity: the same problem keeps the same key across monitor runs
    kind: str
    severity: str  # warning | critical
    title: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    action_taken: str = "Nothing automatic; flagged for a human."
    next_step: str = ""


def aware(dt: datetime | None) -> datetime | None:
    """SQLite drops time zones; everything in this module is UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _hours(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} days"


def robust_z(value: float, history: list[float]) -> float:
    """How unusual `value` is compared to `history`, using median and MAD (robust to outliers)."""
    if len(history) < 3:
        return 0.0
    arr = np.asarray(history, dtype=float)
    median = float(np.median(arr))
    mad = 1.4826 * float(np.median(np.abs(arr - median)))
    scale = max(mad, 0.1 * abs(median), 1e-9)  # a floor, so a perfectly steady history doesn't explode
    return (value - median) / scale


# ---------- data feeds ----------

DATASET_LABELS = {"prices": "Day-ahead prices", "generation": "Generation mix (carbon)", "weather": "Weather forecasts"}


def check_ingest(session, now: datetime) -> list[Finding]:
    findings = []
    for dataset, label in DATASET_LABELS.items():
        runs = session.scalars(
            select(models.IngestRun).where(models.IngestRun.dataset == dataset).order_by(desc(models.IngestRun.id)).limit(30)
        ).all()
        if not runs:
            continue  # never ingested: a fresh install, not an incident

        successes = [r for r in runs if r.status in ("ok", "fallback")]
        last_ok = aware(successes[0].finished_at or successes[0].started_at) if successes else None
        streak = 0
        for r in runs:
            if r.status != "failed":
                break
            streak += 1

        age = now - last_ok if last_ok else None
        stale = age is None or age > timedelta(hours=config.STALE_FEED_HOURS)
        if streak >= config.FAILED_RUNS_ALERT or (stale and streak > 0):
            errors = [f"{aware(r.started_at):%Y-%m-%d %H:%M} UTC · {r.source}: {(r.error or '')[:160]}" for r in runs[:streak][:5]]
            since = f"last success {_hours(age)} ago" if age else "no successful run on record"
            findings.append(
                Finding(
                    key=f"ingest:{dataset}",
                    kind="ingest_failing",
                    severity="critical" if stale else "warning",
                    title=f"{label} feed is failing ({streak} failed runs in a row)",
                    summary=f"The last {streak} ingest attempts for {dataset} failed; {since}.",
                    evidence=errors,
                    action_taken="Retries with backoff on every run; the hourly ingest keeps trying.",
                    next_step="Check the source's status. If it stays down, re-fetch the gap afterwards with "
                    "`python -m app.ingest.cli backfill --days 7 --only-missing`.",
                )
            )
        elif stale:
            # Nothing failed, nothing ran: the ingestor itself is probably stopped.
            findings.append(
                Finding(
                    key=f"ingest:{dataset}",
                    kind="ingest_stopped",
                    severity="warning",
                    title=f"No {label.lower()} ingest for {_hours(age)}: is the ingestor running?",
                    summary=f"The last {dataset} ingest succeeded {_hours(age)} ago and nothing has run since. "
                    "Nothing is failing; the hourly ingest just isn't running.",
                    evidence=[f"Last successful run: {last_ok:%Y-%m-%d %H:%M} UTC ({successes[0].source})"],
                    action_taken="None possible: the ingestor is what fetches data.",
                    next_step="Start it: `python -m app.ingest.cli worker` (or check the ingestor container), "
                    "or run one ingest now with `python -m app.ingest.cli once`.",
                )
            )

    # Fallback mode: prices still arrive, but from the backup source.
    latest_prices = session.scalars(
        select(models.IngestRun).where(models.IngestRun.dataset == "prices").order_by(desc(models.IngestRun.id)).limit(10)
    ).all()
    if latest_prices and latest_prices[0].status == "fallback":
        primary_errors = [
            f"{aware(r.started_at):%Y-%m-%d %H:%M} UTC: {(r.error or '')[:160]}"
            for r in latest_prices
            if r.status == "failed" and r.source == "energy_charts"
        ][:3]
        findings.append(
            Finding(
                key="fallback:prices",
                kind="fallback_active",
                severity="warning",
                title="Primary price source down, running on aWATTar fallback",
                summary="Energy-Charts didn't answer, so prices are coming from aWATTar. Scheduling is unaffected, "
                "but generation data (and so measured carbon) has no fallback.",
                evidence=primary_errors,
                action_taken="Switched to aWATTar automatically; switches back as soon as Energy-Charts answers.",
                next_step="Nothing urgent. If it lasts more than a day, check api.energy-charts.info.",
            )
        )
    return findings


def check_data(session, now: datetime) -> list[Finding]:
    findings = []
    latest_price = aware(session.scalar(select(func.max(models.Price.ts))))
    if latest_price is not None:
        ahead = latest_price + timedelta(hours=1) - now
        if ahead < timedelta(hours=6):
            findings.append(
                Finding(
                    key="data:prices_ahead",
                    kind="data_missing",
                    severity="critical",
                    title="Almost no published prices ahead",
                    summary=f"Published day-ahead prices run out in {_hours(max(ahead, timedelta(0)))}. Normally there are "
                    "at least 11 hours ahead, and about 35 after 13:00.",
                    evidence=[f"Latest published price hour: {latest_price:%Y-%m-%d %H:%M} UTC"],
                    action_taken="The scheduler fills the gap with forecasts, so jobs still get planned.",
                    next_step="Check the ingest feeds; this usually follows a price-feed outage.",
                )
            )

    latest_carbon = aware(session.scalar(select(func.max(models.CarbonIntensity.ts))))
    if latest_carbon is not None and now - latest_carbon > timedelta(hours=6):
        findings.append(
            Finding(
                key="data:carbon_lag",
                kind="data_missing",
                severity="warning",
                title="Carbon measurements are lagging",
                summary=f"The newest measured carbon intensity is {_hours(now - latest_carbon)} old.",
                evidence=[f"Latest carbon hour: {latest_carbon:%Y-%m-%d %H:%M} UTC"],
                action_taken="Recent hours are forecast instead of measured; settlement of finished jobs waits.",
                next_step="Check the generation feed.",
            )
        )

    # Physically impossible values usually mean a unit or parsing bug upstream.
    since = now - timedelta(hours=48)
    bad_prices = session.scalars(
        select(models.Price).where(models.Price.ts >= since, (models.Price.eur_mwh < -500) | (models.Price.eur_mwh > 4000))
    ).all()
    bad_carbon = session.scalars(
        select(models.CarbonIntensity).where(
            models.CarbonIntensity.ts >= since, (models.CarbonIntensity.g_per_kwh < 0) | (models.CarbonIntensity.g_per_kwh > 1200)
        )
    ).all()
    if bad_prices or bad_carbon:
        evidence = [f"price {aware(p.ts):%Y-%m-%d %H:%M}: {p.eur_mwh:.1f} EUR/MWh ({p.source})" for p in bad_prices[:5]]
        evidence += [f"carbon {aware(c.ts):%Y-%m-%d %H:%M}: {c.g_per_kwh:.0f} g/kWh" for c in bad_carbon[:5]]
        findings.append(
            Finding(
                key="data:implausible",
                kind="bad_data",
                severity="critical",
                title="Implausible values in recent data",
                summary="Prices outside the market limits (-500 to 4000 EUR/MWh) or carbon outside 0 to 1200 g/kWh. "
                "That's almost always a unit or parsing change at the source.",
                evidence=evidence,
                next_step="Compare against energy-charts.info; fix the parser, then delete and re-ingest the affected days.",
            )
        )
    return findings


# ---------- jobs ----------


def check_jobs(session, now: datetime) -> list[Finding]:
    findings = []
    overdue = session.scalars(
        select(models.Job).where(models.Job.status == "planned", models.Job.planned_start < now - timedelta(minutes=10))
    ).all()
    if overdue:
        findings.append(
            Finding(
                key="jobs:overdue",
                kind="worker_down",
                severity="critical",
                title=f"{len(overdue)} job(s) overdue: is the scheduler worker running?",
                summary="Jobs are past their planned start but haven't started. The worker that runs them is probably stopped "
                "(or the laptop was asleep).",
                evidence=[f"#{j.id} {j.name}: planned {aware(j.planned_start):%Y-%m-%d %H:%M} UTC, deadline {aware(j.deadline):%H:%M}" for j in overdue[:5]],
                action_taken="None possible: the worker is what acts. When it comes back it runs them, or marks them missed "
                "if the deadline can no longer be met.",
                next_step="Start it: `python -m app.scheduler.cli worker` (or check the scheduler container).",
            )
        )

    day_ago = now - timedelta(hours=24)
    for status, severity, verb in (("missed", "warning", "missed their deadline"), ("failed", "warning", "failed")):
        jobs = session.scalars(
            select(models.Job).where(models.Job.status == status, models.Job.planned_start >= day_ago)
        ).all()
        if jobs:
            findings.append(
                Finding(
                    key=f"jobs:{status}",
                    kind=f"jobs_{status}",
                    severity=severity,
                    title=f"{len(jobs)} job(s) {verb} in the last 24 h",
                    summary=f"See the jobs below; `GET /jobs?status={status}` lists them all.",
                    evidence=[f"#{j.id} {j.name} ({j.task}): {(j.error or '')[:160]}" for j in jobs[:5]],
                    next_step="Look at the error; resubmit the job if it still needs to run.",
                )
            )

    # Unusually slow runs, compared with the same task's history.
    recent = session.scalars(
        select(models.Job).where(models.Job.status == "succeeded", models.Job.actual_start >= day_ago)
    ).all()
    for job in recent:
        if not (job.actual_start and job.actual_end):
            continue
        history = session.scalars(
            select(models.Job).where(
                models.Job.task == job.task, models.Job.status == "succeeded", models.Job.id != job.id, models.Job.actual_end.is_not(None)
            ).order_by(desc(models.Job.id)).limit(30)
        ).all()
        durations = [(aware(h.actual_end) - aware(h.actual_start)).total_seconds() for h in history]
        if len(durations) < 5:
            continue
        took = (aware(job.actual_end) - aware(job.actual_start)).total_seconds()
        z = robust_z(took, durations)
        if z > 4 and took > 30:
            findings.append(
                Finding(
                    key=f"jobs:slow:{job.task}",
                    kind="job_slow",
                    severity="warning",
                    title=f"Task '{job.task}' ran unusually long",
                    summary=f"Job #{job.id} took {took:.0f} s; this task usually takes about {np.median(durations):.0f} s "
                    f"(robust z-score {z:.1f}).",
                    evidence=[f"Last {len(durations)} runs: median {np.median(durations):.0f} s, max {max(durations):.0f} s"],
                    next_step="Check whether the data volume grew or an upstream API got slow.",
                )
            )
    return findings


# ---------- models ----------


def check_models(session, now: datetime) -> list[Finding]:
    if config.FORECAST_MODEL != "lgbm":
        return []
    from app.forecast.live import load_model

    findings = []
    for metric in ("price", "carbon"):
        _, meta = load_model(metric)
        if meta is None:
            findings.append(
                Finding(
                    key=f"model:missing:{metric}",
                    kind="model_missing",
                    severity="warning",
                    title=f"No trained {metric} model; scheduler is using the profile_4w fallback",
                    summary="FORECAST_MODEL is lgbm, but no trained model file exists, so forecasts fall back to profile_4w.",
                    action_taken="Falling back to profile_4w automatically.",
                    next_step="Train it: `python -m app.forecast.cli train`.",
                )
            )
            continue
        age = now - datetime.fromisoformat(meta["trained_at"])
        if age > timedelta(days=config.MODEL_MAX_AGE_DAYS):
            findings.append(
                Finding(
                    key=f"model:stale:{metric}",
                    kind="model_stale",
                    severity="warning",
                    title=f"The {metric} model is {age.days} days old",
                    summary=f"It was trained on data through {meta['trained_through']}. Retraining weekly keeps it current.",
                    next_step="Submit a `retrain_models` job, or run `python -m app.forecast.cli train`.",
                )
            )
    return findings


def run_all(session, now: datetime) -> list[Finding]:
    from app.monitor.drift import check_drift

    findings = []
    for check in (check_ingest, check_data, check_jobs, check_models, check_drift):
        findings.extend(check(session, now))
    return findings
