"""Scheduler commands.

    python -m app.scheduler.cli demo        # submit 5 example jobs and show their plans
    python -m app.scheduler.cli list        # show all jobs
    python -m app.scheduler.cli tick        # one pass: run due jobs, replan, settle
    python -m app.scheduler.cli worker      # run forever (every minute: run due; hourly: replan + settle)
"""

import argparse
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import models
from app.db import SessionLocal, init_db
from app.forecast.baselines import LOCAL_TZ
from app.scheduler.service import JobRejected, create_job, replan_pending, run_due, settle

log = logging.getLogger("gridpilot.scheduler")
BERLIN = ZoneInfo(LOCAL_TZ)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local(ts: datetime | None) -> str:
    return ts.astimezone(BERLIN).strftime("%a %d %b %H:%M") if ts else "-"


def print_jobs(jobs) -> None:
    header = f"{'id':>3}  {'name':<26} {'objective':<9} {'dur':>3}  {'deadline':<16} {'planned start':<16} {'status':<10} {'exp. EUR':>9} {'exp. kg':>8}"
    print(header)
    print("-" * len(header))
    for j in jobs:
        print(
            f"{j.id:>3}  {j.name[:26]:<26} {j.objective:<9} {j.duration_hours:>2}h  {_local(j.deadline):<16} "
            f"{_local(j.planned_start):<16} {j.status:<10} {j.expected_cost_eur or 0:>9.4f} {j.expected_co2_kg or 0:>8.3f}"
        )
    print("\nTimes are Berlin time. Ask why with:  GET /jobs/<id>/explain")


def cmd_demo() -> None:
    init_db()
    now = _now()
    local_now = now.astimezone(BERLIN)
    tomorrow_8 = (local_now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    tomorrow_noon = tomorrow_8.replace(hour=12)
    demo_jobs = [
        {"name": "Nightly report refresh", "task": "demo", "duration_hours": 2, "deadline": tomorrow_8, "objective": "cost"},
        {"name": "Weekly model retrain", "task": "retrain_models", "duration_hours": 4, "power_kw": 5.0, "deadline": now + timedelta(hours=48), "objective": "carbon"},
        {"name": "Database backup", "task": "demo", "duration_hours": 1, "deadline": now + timedelta(hours=12), "objective": "blend", "carbon_weight": 0.5},
        {"name": "Refresh forecasts", "task": "refresh_forecasts", "duration_hours": 1, "deadline": tomorrow_noon, "objective": "cost"},
        {"name": "Ingest catch-up", "task": "ingest", "duration_hours": 1, "deadline": now + timedelta(hours=6), "objective": "carbon"},
    ]
    created = []
    with SessionLocal() as session:
        for data in demo_jobs:
            data["deadline"] = data["deadline"].astimezone(timezone.utc)
            try:
                created.append(create_job(session, data, now))
            except JobRejected as exc:
                print(f"[SKIP] {data['name']}: {exc}")
        print()
        print_jobs(created)


def cmd_list() -> None:
    init_db()
    with SessionLocal() as session:
        jobs = session.scalars(select(models.Job).order_by(models.Job.id)).all()
        if not jobs:
            print("No jobs yet. Try:  python -m app.scheduler.cli demo")
            return
        print_jobs(jobs)


def tick(replan: bool = True) -> None:
    now = _now()
    with SessionLocal() as session:
        ran = run_due(session, now)
        moved = replan_pending(session, now) if replan else 0
        settled = settle(session, now) if replan else 0
    log.info("tick: ran %s, replanned %d moved, settled %d", ran or "nothing", moved, settled)


def monitor() -> None:
    from app.monitor.incidents import run_monitor

    with SessionLocal() as session:
        result = run_monitor(session, _now())
    log.info("monitor: %s", {k: len(v) for k, v in result.items()})


def submit_weekly_retrain(now: datetime | None = None) -> models.Job | None:
    """Every Saturday: ask GridPilot to retrain its own models, in the cleanest window of the weekend.

    Skipped if a retrain job is already waiting, so a restarted worker never queues two.
    """
    now = now or _now()
    with SessionLocal() as session:
        pending = session.scalar(
            select(models.Job).where(models.Job.task == "retrain_models", models.Job.status.in_(["planned", "running"]))
        )
        if pending is not None:
            log.info("Retrain job #%s already pending; not adding another", pending.id)
            return None
        job = create_job(
            session,
            {
                "name": "Weekly model retrain",
                "task": "retrain_models",
                "duration_hours": 2,
                "power_kw": 5.0,
                "deadline": now + timedelta(hours=40),
                "objective": "carbon",
            },
            now,
        )
        log.info("Weekly retrain planned for %s", job.planned_start)
        return job


def store_day_ahead_forecast() -> None:
    """Keep the forecast the scheduler plans with, so drift detection can score it tomorrow."""
    from app.scheduler.tasks import TASKS

    with SessionLocal() as session:
        log.info(TASKS["refresh_forecasts"].run(session))


def cmd_worker() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    init_db()
    tick()
    monitor()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(lambda: tick(replan=False), "cron", second=30, id="run-due", max_instances=1, coalesce=True)
    # :10 past the hour, after the ingestor's :05 run has brought in fresh data
    scheduler.add_job(tick, "cron", minute=10, second=45, id="replan-settle", max_instances=1, coalesce=True)
    scheduler.add_job(monitor, "cron", minute="*/15", second=50, id="monitor", max_instances=1, coalesce=True)
    scheduler.add_job(
        store_day_ahead_forecast, "cron", hour=10, minute=5, timezone=BERLIN, id="day-ahead-forecast", max_instances=1, coalesce=True
    )
    scheduler.add_job(
        submit_weekly_retrain, "cron", day_of_week="sat", hour=6, timezone=BERLIN, id="weekly-retrain", max_instances=1, coalesce=True
    )
    log.info("Scheduler worker started: due jobs every minute, replan + settle hourly, health checks every 15 min, "
             "day-ahead forecast stored daily at 10:05 Berlin, model retraining submitted every Saturday")
    scheduler.start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="gridpilot-scheduler")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "list", "tick", "worker"):
        sub.add_parser(name)
    args = parser.parse_args()
    {"demo": cmd_demo, "list": cmd_list, "tick": tick, "worker": cmd_worker}[args.command]()


if __name__ == "__main__":
    main()
