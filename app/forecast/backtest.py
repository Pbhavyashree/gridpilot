"""Walk-forward backtest: replay history one day at a time and score each model.

Two scores, because GridPilot cares about two different things:

1. MAE (mean absolute error): how far off the forecast is, hour by hour.
2. Window score: what the scheduler actually does with a forecast. For each day we
   pick the cheapest contiguous k-hour window according to the forecast, then check
   against the real values:
     - hit rate: how often that was truly the best window
     - regret: how much worse (in EUR/MWh or g/kWh, averaged over the window) the
       chosen window was than the best one. Lower is better; 0 is perfect.

A forecast can have a mediocre MAE and still be great for scheduling (it only needs
to get the SHAPE of the day right), which is why we track both.

"fixed_00:00" is the no-forecast reference: always start the job at midnight,
like a plain cron job would. Every model should beat it on regret.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.forecast.baselines import MODELS, forecast_day

FIXED_START_HOUR = 0


def run_backtest(matrix: pd.DataFrame, metric: str, first_target: date, last_target: date) -> pd.DataFrame:
    """Long format: target_day, hour, model, forecast, actual."""
    records = []
    day = first_target
    while day <= last_target:
        actual = matrix.loc[day].to_numpy(dtype=float) if day in matrix.index else np.full(24, np.nan)
        for model in MODELS:
            forecast = forecast_day(matrix, metric, day, model)
            for hour in range(24):
                records.append((day, hour, model, forecast[hour], actual[hour]))
        day += timedelta(days=1)
    return pd.DataFrame(records, columns=["target_day", "hour", "model", "forecast", "actual"])


def best_window_start(values: np.ndarray, k: int) -> int:
    sums = np.convolve(values, np.ones(k), mode="valid")
    return int(np.argmin(sums))


def window_scores(predictions: pd.DataFrame, k: int) -> pd.DataFrame:
    """Per model: days scored, hit rate, mean regret. Only days where every model and the actuals are complete."""
    wide = predictions.pivot_table(index=["target_day", "hour"], columns="model", values="forecast")
    actual = predictions.drop_duplicates(["target_day", "hour"]).set_index(["target_day", "hour"])["actual"]
    wide["actual"] = actual

    fixed_hours = range(24 - k + 1)
    results = {name: {"hits": 0, "regret": []} for name in [*MODELS, *(f"fixed_{h:02d}:00" for h in fixed_hours)]}
    for _, day in wide.groupby(level="target_day"):
        if len(day) < 24 or day.isna().any().any():
            continue  # incomplete data, or the 23-hour DST day
        truth = day["actual"].to_numpy()
        best = best_window_start(truth, k)
        best_mean = truth[best : best + k].mean()

        choices = {model: best_window_start(day[model].to_numpy(), k) for model in MODELS}
        choices.update({f"fixed_{h:02d}:00": h for h in fixed_hours})

        for name, start in choices.items():
            results[name]["hits"] += int(start == best)
            results[name]["regret"].append(truth[start : start + k].mean() - best_mean)

    # Keep two fixed-time references: midnight (a typical cron job), and the single best
    # fixed hour IN HINDSIGHT. The second one cheats (it was picked after seeing the whole
    # period), so beating it is the convincing result.
    fixed = {name: r for name, r in results.items() if name.startswith("fixed_")}
    best_fixed = min(fixed, key=lambda name: np.mean(fixed[name]["regret"]) if fixed[name]["regret"] else np.inf)
    midnight = f"fixed_{FIXED_START_HOUR:02d}:00"
    results = {name: r for name, r in results.items() if not name.startswith("fixed_")}
    results[midnight] = fixed[midnight]
    results[f"{best_fixed} (best in hindsight)"] = fixed[best_fixed]

    rows = []
    for name, r in results.items():
        n = len(r["regret"])
        rows.append(
            {
                "model": name,
                "days": n,
                "hit_rate": r["hits"] / n if n else np.nan,
                "regret": float(np.mean(r["regret"])) if n else np.nan,
            }
        )
    return pd.DataFrame(rows)


def mae_scores(predictions: pd.DataFrame) -> pd.DataFrame:
    """Per model MAE, on the hours where EVERY model had a forecast (a fair comparison)."""
    wide = predictions.pivot_table(index=["target_day", "hour"], columns="model", values="forecast")
    actual = predictions.drop_duplicates(["target_day", "hour"]).set_index(["target_day", "hour"])["actual"]
    mask = wide.notna().all(axis=1) & actual.reindex(wide.index).notna()
    wide, actual = wide[mask], actual.reindex(wide.index)[mask]
    return pd.DataFrame(
        [{"model": m, "hours": int(mask.sum()), "mae": float((wide[m] - actual).abs().mean())} for m in MODELS]
    )


def scoreboard(predictions: pd.DataFrame, k: int) -> pd.DataFrame:
    board = window_scores(predictions, k).merge(mae_scores(predictions), on="model", how="left")
    return board[["model", "mae", "hit_rate", "regret", "days", "hours"]].sort_values("regret").reset_index(drop=True)


def to_markdown(board: pd.DataFrame, metric: str, unit: str, k: int, first: date, last: date) -> str:
    lines = [
        f"**{metric.title()} forecast backtest** ({first} to {last}, {k}-hour window)",
        "",
        f"| Model | MAE ({unit}) | Best window found | Regret ({unit}) |",
        "| --- | --- | --- | --- |",
    ]
    for row in board.itertuples():
        mae = "–" if pd.isna(row.mae) else f"{row.mae:.1f}"
        hit = "–" if pd.isna(row.hit_rate) else f"{row.hit_rate:.0%}"
        regret = "–" if pd.isna(row.regret) else f"{row.regret:.1f}"
        lines.append(f"| {row.model} | {mae} | {hit} | {regret} |")
    days = int(board["days"].max()) if len(board) else 0
    lines += ["", f"_{days} complete days scored. Regret = how much worse the chosen window was than the best one._"]
    return "\n".join(lines)
