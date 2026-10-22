"""One-year savings simulation: what would GridPilot have saved on realistic jobs?

Every day, each job below must run once somewhere inside that day (Berlin time).
Each strategy picks a start hour; the job is then charged at the REAL prices and
carbon intensity of the hours it ran.

Strategies:
  cron 00:00            no scheduler at all: a plain nightly cron job
  fixed HH:00           the single best start hour per job, chosen in hindsight (cheats in its favour)
  <forecast model>      GridPilot planning on that model's forecast (forecast made at 10:00 the day before)
  perfect foresight     planning on the real values: the theoretical best, unreachable in practice

The forecast strategies use app.scheduler.placement.place, the exact code the live
scheduler uses, so this measures the real decision logic, not a re-implementation.

Note: for price this is conservative. Live, tomorrow's real prices are published around
13:00, and the scheduler uses them instead of a forecast whenever a job is planned later.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.scheduler.placement import place

BASE = datetime(2000, 1, 1, tzinfo=timezone.utc)  # placement only needs positions 0..23


@dataclass(frozen=True)
class SimJob:
    name: str
    hours: int
    power_kw: float
    objective: str  # cost | carbon | blend
    carbon_weight: float = 0.5


JOBS = [
    SimJob("Nightly data refresh", 2, 1.0, "cost"),
    SimJob("Model retraining", 4, 5.0, "carbon"),
    SimJob("Backups", 1, 0.5, "blend"),
]


def _day_curves(price_pred: pd.DataFrame, carbon_pred: pd.DataFrame) -> tuple[dict, list[str]]:
    """{day: {"actual": (price[24], carbon[24]), model: (price[24], carbon[24])}} for complete days only."""
    models = [m for m in dict.fromkeys(price_pred["model"]) if m in set(carbon_pred["model"])]
    p = price_pred.pivot_table(index=["target_day", "hour"], columns="model", values="forecast", dropna=False)
    c = carbon_pred.pivot_table(index=["target_day", "hour"], columns="model", values="forecast", dropna=False)
    pa = price_pred.drop_duplicates(["target_day", "hour"]).set_index(["target_day", "hour"])["actual"]
    ca = carbon_pred.drop_duplicates(["target_day", "hour"]).set_index(["target_day", "hour"])["actual"]

    days = {}
    for day in sorted(set(p.index.get_level_values(0)) & set(c.index.get_level_values(0))):
        try:
            entry = {"actual": (pa.loc[day].to_numpy(dtype=float), ca.loc[day].to_numpy(dtype=float))}
            for m in models:
                entry[m] = (p.loc[day, m].to_numpy(dtype=float), c.loc[day, m].to_numpy(dtype=float))
        except KeyError:
            continue
        if all(len(a) == 24 and len(b) == 24 and not np.isnan(a).any() and not np.isnan(b).any() for a, b in entry.values()):
            days[day] = entry
    return days, models


def _choose(price: np.ndarray, carbon: np.ndarray, job: SimJob) -> int:
    curve = pd.DataFrame({"ts": [BASE + timedelta(hours=h) for h in range(24)], "eur_mwh": price, "g_per_kwh": carbon})
    p = place(curve, job.hours, BASE, BASE + timedelta(hours=24), job.objective, job.carbon_weight, job.power_kw)
    return int((p.chosen.start - BASE) / timedelta(hours=1))


def _charge(actual: tuple[np.ndarray, np.ndarray], start: int, job: SimJob) -> tuple[float, float]:
    price, carbon = actual
    window = slice(start, start + job.hours)
    return float(price[window].sum()) * job.power_kw / 1000, float(carbon[window].sum()) * job.power_kw / 1000


def simulate(price_pred: pd.DataFrame, carbon_pred: pd.DataFrame, jobs: list[SimJob] = JOBS) -> tuple[pd.DataFrame, int]:
    days, models = _day_curves(price_pred, carbon_pred)
    totals: dict[str, list[float]] = {}

    def add(strategy: str, eur: float, kg: float) -> None:
        totals.setdefault(strategy, [0.0, 0.0])
        totals[strategy][0] += eur
        totals[strategy][1] += kg

    for job in jobs:
        # Best fixed start hour in hindsight, for this job's own objective.
        per_hour = np.array([[_charge(d["actual"], h, job) for d in days.values()] for h in range(24 - job.hours + 1)])
        eur, kg = per_hour[..., 0].sum(axis=1), per_hour[..., 1].sum(axis=1)
        if job.objective == "cost":
            score = eur
        elif job.objective == "carbon":
            score = kg
        else:
            score = (1 - job.carbon_weight) * eur / eur.mean() + job.carbon_weight * kg / kg.mean()
        best_fixed = int(np.argmin(score))

        for entry in days.values():
            add("cron 00:00", *_charge(entry["actual"], 0, job))
            add("best fixed hour (hindsight)", *_charge(entry["actual"], best_fixed, job))
            for m in models:
                add(m, *_charge(entry["actual"], _choose(*entry[m], job), job))
            add("perfect foresight", *_charge(entry["actual"], _choose(*entry["actual"], job), job))

    cron_eur, cron_kg = totals["cron 00:00"]
    best_eur, best_kg = totals["perfect foresight"]
    rows = []
    for strategy, (eur, kg) in totals.items():
        rows.append(
            {
                "strategy": strategy,
                "cost_eur": eur,
                "co2_kg": kg,
                "cost_saved_pct": 100 * (cron_eur - eur) / cron_eur if cron_eur else np.nan,
                "co2_saved_pct": 100 * (cron_kg - kg) / cron_kg if cron_kg else np.nan,
                "share_of_possible_cost": 100 * (cron_eur - eur) / (cron_eur - best_eur) if cron_eur != best_eur else np.nan,
                "share_of_possible_co2": 100 * (cron_kg - kg) / (cron_kg - best_kg) if cron_kg != best_kg else np.nan,
            }
        )
    order = ["cron 00:00", "best fixed hour (hindsight)", *models, "perfect foresight"]
    table = pd.DataFrame(rows).set_index("strategy").loc[order].reset_index()
    return table, len(days)


def to_markdown(table: pd.DataFrame, n_days: int, jobs: list[SimJob] = JOBS) -> str:
    job_text = "; ".join(f"{j.name} ({j.hours} h, {j.power_kw:g} kW, {j.objective})" for j in jobs)
    lines = [
        f"**One-year savings simulation** ({n_days} days, every job once per day)",
        "",
        f"Jobs: {job_text}.",
        "",
        "| Strategy | Cost (EUR) | CO₂ (kg) | Cost saved vs cron | CO₂ saved vs cron | Share of possible saving (cost / CO₂) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in table.itertuples():
        share = "–" if r.strategy == "cron 00:00" else f"{r.share_of_possible_cost:.0f}% / {r.share_of_possible_co2:.0f}%"
        lines.append(
            f"| {r.strategy} | {r.cost_eur:,.0f} | {r.co2_kg:,.0f} | {r.cost_saved_pct:.1f}% | {r.co2_saved_pct:.1f}% | {share} |"
        )
    lines += [
        "",
        "_Each job is charged at the real prices and carbon intensity of the hours it actually ran. "
        "Forecast strategies use the same placement code as the live scheduler._",
    ]
    return "\n".join(lines)
