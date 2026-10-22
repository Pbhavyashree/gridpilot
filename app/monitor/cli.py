"""Monitoring commands.

    python -m app.monitor.cli check          # run all health checks once; open / update / resolve incidents
    python -m app.monitor.cli incidents      # list incidents (open first)
    python -m app.monitor.cli show <id>      # print one incident's full write-up
"""

import argparse
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select

from app import models
from app.db import SessionLocal, init_db
from app.forecast.baselines import LOCAL_TZ
from app.monitor.incidents import run_monitor

BERLIN = ZoneInfo(LOCAL_TZ)
ICON = {"critical": "[CRIT]", "warning": "[WARN]"}


def cmd_check() -> None:
    init_db()
    with SessionLocal() as session:
        result = run_monitor(session, datetime.now(timezone.utc))
        for label, items in result.items():
            for i in items:
                print(f"{label:<9} #{i.id:<4} {ICON.get(i.severity, '')} {i.title}")
        open_now = session.scalars(select(models.Incident).where(models.Incident.status == "open")).all()
        if not open_now:
            print("[OK]   All checks passed: no open incidents.")
        else:
            print(f"\n{len(open_now)} open incident(s). Read one with:  python -m app.monitor.cli show <id>")


def cmd_incidents() -> None:
    init_db()
    with SessionLocal() as session:
        incidents = session.scalars(select(models.Incident).order_by(models.Incident.status, desc(models.Incident.id)).limit(30)).all()
        if not incidents:
            print("No incidents recorded. Run:  python -m app.monitor.cli check")
            return
        for i in incidents:
            since = i.first_seen.astimezone(BERLIN).strftime("%a %d %b %H:%M")
            print(f"#{i.id:<4} {i.status:<9} {ICON.get(i.severity, ''):<7} since {since}  {i.title}")


def cmd_show(incident_id: int) -> None:
    init_db()
    with SessionLocal() as session:
        incident = session.get(models.Incident, incident_id)
        print(incident.note if incident else f"No incident #{incident_id}.")


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="gridpilot-monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("incidents")
    show = sub.add_parser("show")
    show.add_argument("id", type=int)
    args = parser.parse_args()
    if args.command == "check":
        cmd_check()
    elif args.command == "incidents":
        cmd_incidents()
    else:
        cmd_show(args.id)


if __name__ == "__main__":
    main()
