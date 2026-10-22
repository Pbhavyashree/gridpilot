"""Turn findings into incidents, and write each one up like an on-call note.

    new finding            -> open an incident (and post to the webhook, if configured)
    finding seen again     -> update it (last_seen, evidence, severity)
    finding gone           -> resolve it (and post the resolution)

One open incident per key, so an outage that lasts 10 hours is one incident, not 40.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app import config, models
from app.forecast.baselines import LOCAL_TZ
from app.monitor.checks import Finding, aware

log = logging.getLogger(__name__)
BERLIN = ZoneInfo(LOCAL_TZ)


def _local(ts: datetime) -> str:
    return aware(ts).astimezone(BERLIN).strftime("%a %d %b %H:%M")


def render_note(f: Finding, first_seen: datetime, last_seen: datetime, checks: int) -> str:
    lines = [
        f"## {f.title}",
        "",
        f"**Severity:** {f.severity} · **Since:** {_local(first_seen)} (Berlin) · "
        f"**Last checked:** {_local(last_seen)} · seen in {checks} check(s)",
        "",
        f.summary,
    ]
    if f.evidence:
        lines += ["", "**Evidence**", *[f"- {e}" for e in f.evidence]]
    lines += ["", f"**What GridPilot did:** {f.action_taken}"]
    if f.next_step:
        lines += [f"**Next step:** {f.next_step}"]
    return "\n".join(lines)


def notify(text: str) -> None:
    """Post to a Discord or Slack incoming webhook. Never raises: alerting must not break monitoring."""
    url = config.ALERT_WEBHOOK_URL
    if not url:
        return
    payload = {"content": text[:1900]} if "discord" in url else {"text": text}
    try:
        httpx.post(url, json=payload, timeout=10).raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Webhook notification failed: %s", exc)


def sync(session, findings: list[Finding], now: datetime) -> dict[str, list[models.Incident]]:
    open_incidents = {i.key: i for i in session.scalars(select(models.Incident).where(models.Incident.status == "open"))}
    seen = set()
    opened, updated, resolved = [], [], []

    for f in findings:
        if f.key in seen:
            continue  # two checks reporting the same key in one run: keep the first
        seen.add(f.key)
        incident = open_incidents.get(f.key)
        if incident is None:
            incident = models.Incident(
                key=f.key,
                kind=f.kind,
                severity=f.severity,
                status="open",
                title=f.title,
                note=render_note(f, now, now, 1),
                first_seen=now,
                last_seen=now,
                checks=1,
            )
            session.add(incident)
            opened.append(incident)
        else:
            incident.checks += 1
            incident.last_seen = now
            incident.severity = f.severity
            incident.title = f.title
            incident.note = render_note(f, incident.first_seen, now, incident.checks)
            updated.append(incident)

    for key, incident in open_incidents.items():
        if key not in seen:
            incident.status = "resolved"
            incident.resolved_at = now
            duration = now - aware(incident.first_seen)
            incident.note += f"\n\n**Resolved:** {_local(now)}, after {duration.total_seconds() / 3600:.1f} h."
            resolved.append(incident)

    session.commit()
    for i in opened:
        log.warning("Incident opened: [%s] %s", i.severity, i.title)
        notify(f"🔴 GridPilot [{i.severity}] {i.title}")
    for i in resolved:
        log.info("Incident resolved: %s", i.title)
        notify(f"✅ GridPilot resolved: {i.title}")
    return {"opened": opened, "updated": updated, "resolved": resolved}


def run_monitor(session, now: datetime) -> dict[str, list[models.Incident]]:
    from app.monitor.checks import run_all

    return sync(session, run_all(session, now), now)
