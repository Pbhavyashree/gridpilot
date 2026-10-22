"""Command line entry points.

    python -m app.ingest.cli check                     # live test: can we reach both APIs?
    python -m app.ingest.cli once                      # one ingest run (last 2 days + tomorrow)
    python -m app.ingest.cli backfill --days 730       # load history
    python -m app.ingest.cli coverage --days 730       # which days are incomplete?
    python -m app.ingest.cli backfill --days 730 --only-missing   # re-fetch just the gaps
    python -m app.ingest.cli worker                    # run forever, ingest every hour
"""

import argparse
import logging
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal, init_db
from app.ingest import awattar, energy_charts
from app.ingest.coverage import find_gaps, to_ranges
from app.ingest.http import make_client
from app.ingest.pipeline import backfill, backfill_missing, run_once

log = logging.getLogger("gridpilot.ingest")


def cmd_check() -> None:
    """Hit each API once and print what came back. Writes nothing to the database."""
    today = datetime.now(timezone.utc).date()
    with make_client() as client:
        for label, fetch in (
            ("Energy-Charts prices", lambda: energy_charts.fetch_prices(client, today, today)),
            ("Energy-Charts generation", lambda: energy_charts.fetch_generation(client, today, today)),
            (
                "aWATTar prices",
                lambda: awattar.fetch_prices(
                    client, datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(hours=24)
                ),
            ),
        ):
            try:
                df = fetch()
                print(f"[OK]   {label}: {len(df)} rows")
                print(df.head(3).to_string(index=False), "\n")
            except Exception as exc:  # noqa: BLE001  (we want to see any failure here)
                print(f"[FAIL] {label}: {exc}\n")


def cmd_once() -> None:
    init_db()
    with SessionLocal() as session, make_client() as client:
        run_once(session, client)
    log.info("Ingest run finished")


def cmd_backfill(days: int, only_missing: bool) -> None:
    init_db()
    with SessionLocal() as session, make_client() as client:
        if only_missing:
            missing = backfill_missing(session, client, days)
            log.info("Re-fetched incomplete days: %s", missing)
        else:
            backfill(session, client, days)
    log.info("Backfill finished. Run 'coverage' to check for remaining gaps.")


def cmd_coverage(days: int) -> None:
    init_db()
    with SessionLocal() as session:
        gaps = find_gaps(session, days)
    for dataset, missing in gaps.items():
        if not missing:
            print(f"[OK]   {dataset}: all {days} days complete")
            continue
        print(f"[GAPS] {dataset}: {len(missing)} incomplete day(s) out of {days}")
        for start, end in to_ranges(missing):
            print(f"         {start}" + ("" if start == end else f" to {end}"))
    if any(gaps.values()):
        print(f"\nFix with:  python -m app.ingest.cli backfill --days {days} --only-missing")


def cmd_worker() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    init_db()
    cmd_once()  # run immediately on start, then every hour at minute 5
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(cmd_once, "cron", minute=5, id="hourly-ingest", max_instances=1, coalesce=True)
    log.info("Worker started; ingesting every hour at :05 UTC")
    scheduler.start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is too noisy
    parser = argparse.ArgumentParser(prog="gridpilot-ingest")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("once")
    bf = sub.add_parser("backfill")
    bf.add_argument("--days", type=int, default=730)
    bf.add_argument("--only-missing", action="store_true", help="re-fetch only incomplete days")
    cov = sub.add_parser("coverage")
    cov.add_argument("--days", type=int, default=730)
    sub.add_parser("worker")
    args = parser.parse_args()

    if args.command == "check":
        cmd_check()
    elif args.command == "once":
        cmd_once()
    elif args.command == "backfill":
        cmd_backfill(args.days, args.only_missing)
    elif args.command == "coverage":
        cmd_coverage(args.days)
    elif args.command == "worker":
        cmd_worker()


if __name__ == "__main__":
    main()
