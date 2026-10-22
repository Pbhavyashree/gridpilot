"""Where should a job run? A pure function: curve in, chosen window out. No database.

The job occupies `duration_hours` consecutive hours somewhere between its earliest
start and its deadline. We slide that window across every possible start hour and
pick the one with the lowest score:

    cost    -> lowest expected electricity cost (EUR)
    carbon  -> lowest expected emissions (kg CO2)
    blend   -> a weighted mix of both, each scaled 0..1 across the candidate windows
               so euros and kilograms can be compared at all

Ties go to the earliest start (finishing sooner is never worse).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

Objective = str  # "cost" | "carbon" | "blend"


class InfeasibleJob(ValueError):
    """No window of the required length fits between earliest start and deadline."""


@dataclass
class Window:
    start: datetime
    end: datetime
    cost_eur: float
    co2_kg: float


@dataclass
class Placement:
    chosen: Window
    asap: Window  # what "just run it now" would have cost: the comparison for /explain
    candidates: int
    objective: Objective
    carbon_weight: float
    scores: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def savings(self) -> dict:
        def pct(new: float, old: float) -> float | None:
            return None if old == 0 else round(100 * (old - new) / abs(old), 1)

        return {
            "cost_eur": round(self.asap.cost_eur - self.chosen.cost_eur, 4),
            "cost_pct": pct(self.chosen.cost_eur, self.asap.cost_eur),
            "co2_kg": round(self.asap.co2_kg - self.chosen.co2_kg, 4),
            "co2_pct": pct(self.chosen.co2_kg, self.asap.co2_kg),
        }


def ceil_hour(ts: datetime) -> datetime:
    floored = ts.replace(minute=0, second=0, microsecond=0)
    return floored if floored == ts else floored + timedelta(hours=1)


def place(
    curve: pd.DataFrame,
    duration_hours: int,
    earliest_start: datetime,
    deadline: datetime,
    objective: Objective = "cost",
    carbon_weight: float = 0.5,
    power_kw: float = 1.0,
) -> Placement:
    """curve: hourly rows with columns ts (UTC, hour start), eur_mwh, g_per_kwh.

    A window is only a candidate if every hour in it has both a price and a carbon value.
    """
    if duration_hours < 1:
        raise ValueError("duration_hours must be at least 1")
    if objective == "cost":
        weight = 0.0
    elif objective == "carbon":
        weight = 1.0
    elif objective == "blend":
        if not 0.0 <= carbon_weight <= 1.0:
            raise ValueError("carbon_weight must be between 0 and 1")
        weight = carbon_weight
    else:
        raise ValueError(f"unknown objective: {objective}")

    first_start = ceil_hour(earliest_start)
    last_start = deadline - timedelta(hours=duration_hours)
    if last_start < first_start:
        raise InfeasibleJob(
            f"a {duration_hours}h job can't fit between {first_start:%Y-%m-%d %H:%M} and {deadline:%Y-%m-%d %H:%M} UTC"
        )

    data = curve.copy()
    data["ts"] = pd.to_datetime(data["ts"], utc=True)
    data = data.set_index("ts").sort_index()
    hours = pd.date_range(first_start, deadline, freq="1h", inclusive="left")
    data = data.reindex(hours)

    # Energy per hour: price (EUR/MWh) * kW * 1h / 1000 = EUR;  g/kWh * kW * 1h / 1000 = kg
    cost = data["eur_mwh"].to_numpy(dtype=float) * power_kw / 1000
    co2 = data["g_per_kwh"].to_numpy(dtype=float) * power_kw / 1000

    n_starts = int((last_start - first_start) / timedelta(hours=1)) + 1
    window_cost = np.array([cost[i : i + duration_hours].sum() for i in range(n_starts)])
    window_co2 = np.array([co2[i : i + duration_hours].sum() for i in range(n_starts)])
    complete = np.array(
        [not np.isnan(cost[i : i + duration_hours]).any() and not np.isnan(co2[i : i + duration_hours]).any() for i in range(n_starts)]
    )
    if not complete.any():
        raise InfeasibleJob("no price/carbon data (published or forecast) covers any possible window")

    def scaled(values: np.ndarray) -> np.ndarray:
        valid = values[complete]
        span = valid.max() - valid.min()
        return np.zeros_like(values) if span == 0 else (values - valid.min()) / span

    score = (1 - weight) * scaled(window_cost) + weight * scaled(window_co2)
    score = np.where(complete, score, np.inf)
    best = int(np.argmin(score))  # argmin returns the first minimum -> earliest start wins ties
    asap = int(np.argmax(complete))  # first complete window

    def window(i: int) -> Window:
        start = (first_start + timedelta(hours=i)).replace(tzinfo=first_start.tzinfo)
        return Window(
            start=start,
            end=start + timedelta(hours=duration_hours),
            cost_eur=round(float(window_cost[i]), 4),
            co2_kg=round(float(window_co2[i]), 4),
        )

    return Placement(
        chosen=window(best),
        asap=window(asap),
        candidates=int(complete.sum()),
        objective=objective,
        carbon_weight=weight,
        scores=[(first_start + timedelta(hours=i), float(s)) for i, s in enumerate(score) if np.isfinite(s)],
    )
