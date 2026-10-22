"""The ONLY things a job is allowed to run.

Jobs name a task from this registry; they can never pass a shell command or code.
That matters once the API is on the internet: a scheduler that runs arbitrary
commands is a remote-code-execution hole with a nice UI.

Each task gets a database session and returns a short message for the job record.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Task:
    description: str
    run: Callable[..., str]


def _demo(session) -> str:
    log.info("Demo task ran")
    return "demo task finished"


def _refresh_forecasts(session) -> str:
    from app import config
    from app.forecast.baselines import LOCAL_TZ
    from app.forecast.service import make_and_store_forecast

    tomorrow = datetime.now(ZoneInfo(LOCAL_TZ)).date() + timedelta(days=1)
    counts = {m: len(make_and_store_forecast(session, m, config.FORECAST_MODEL, tomorrow)) for m in ("price", "carbon")}
    return f"stored forecasts for {tomorrow}: {counts}"


def _ingest(session) -> str:
    from app.ingest.http import make_client
    from app.ingest.pipeline import run_once

    with make_client() as client:
        run_once(session, client)
    return "ingest run finished"


def _retrain_models(session) -> str:
    from app.forecast.live import train_and_save

    results = []
    for metric in ("price", "carbon"):
        meta = train_and_save(session, metric)
        results.append(f"{metric}: {meta['train_days']} days through {meta['trained_through']}")
    return "retrained lgbm: " + "; ".join(results)


TASKS: dict[str, Task] = {
    "demo": Task("Does nothing but log; for trying the scheduler out", _demo),
    "refresh_forecasts": Task("Forecast tomorrow's price and carbon and store it", _refresh_forecasts),
    "ingest": Task("Run one data ingest (last 2 days + tomorrow)", _ingest),
    "retrain_models": Task("Retrain the lgbm forecasters on all data so far", _retrain_models),
}
