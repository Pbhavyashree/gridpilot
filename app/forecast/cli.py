"""Forecasting commands.

    python -m app.forecast.cli backtest --days 365            # baselines + lgbm (if weather is loaded) + savings simulation
    python -m app.forecast.cli backtest --days 365 --no-ml    # baselines only (fast)
    python -m app.forecast.cli backtest --days 365 --window 3
    python -m app.forecast.cli forecast --model profile_4w    # forecast tomorrow and save it
"""

import argparse
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app.db import SessionLocal, init_db
from app.forecast import ml, simulate
from app.forecast.backtest import run_backtest, scoreboard, to_markdown
from app.forecast.baselines import LOCAL_TZ, METRIC_UNITS, MODELS
from app.forecast.service import load_matrix, make_and_store_forecast

REPORTS_DIR = Path("reports")
WARMUP_DAYS = 35  # history the baselines need before the first scored day (4 weeks + margin)


def local_today() -> date:
    return datetime.now(ZoneInfo(LOCAL_TZ)).date()


def _ml_predictions(session, metric: str, full_matrix, start: date, last: date):
    """Walk-forward LightGBM predictions, or None if there's no weather data to learn from."""
    data_first = full_matrix.index.min()
    weather = ml.load_weather(session, data_first, last)
    if weather.empty:
        print(f"[INFO] {metric}: no weather data, skipping lgbm. Load it with:  python -m app.ingest.cli weather --days 730")
        return None, None
    print(f"[....] {metric}: training lgbm walk-forward (retrained every {ml.RETRAIN_EVERY_DAYS} days), this takes a minute")
    features = ml.build_features(full_matrix, metric, ml.weather_matrices(weather), data_first + timedelta(days=WARMUP_DAYS), last)
    return ml.walk_forward(features, metric, start, last)


def cmd_backtest(days: int, window: int, metrics: list[str], use_ml: bool = True) -> None:
    init_db()
    REPORTS_DIR.mkdir(exist_ok=True)
    last = local_today() - timedelta(days=1)
    first = last - timedelta(days=days - 1)
    sections, all_predictions = [], {}

    with SessionLocal() as session:
        for metric in metrics:
            # Load all history: the baselines need 4 weeks, lgbm needs months to train on.
            matrix = load_matrix(session, metric, date(2015, 1, 1), last)
            if matrix.empty:
                print(f"[SKIP] {metric}: no data loaded. Run the ingest backfill first.")
                continue
            data_first = matrix.index.min()
            start = max(first, data_first + timedelta(days=WARMUP_DAYS + ml.MIN_TRAIN_DAYS))
            if start > last:
                print(f"[SKIP] {metric}: not enough history yet (data starts {data_first}).")
                continue

            predictions = run_backtest(matrix, metric, start, last)
            if use_ml:
                ml_pred, importance = _ml_predictions(session, metric, matrix, start, last)
                if ml_pred is not None and not ml_pred.empty:
                    predictions = pd.concat([predictions, ml_pred], ignore_index=True)
                    importance.to_csv(REPORTS_DIR / f"feature_importance_{metric}.csv", index=False)
                    top = ", ".join(f"{r.feature} {r.gain_pct:.0f}%" for r in importance.head(6).itertuples())
                    print(f"[OK]   {metric}: lgbm top features: {top}")

            predictions.to_csv(REPORTS_DIR / f"backtest_{metric}.csv", index=False)
            all_predictions[metric] = predictions
            board = scoreboard(predictions, window)
            md = to_markdown(board, metric, METRIC_UNITS[metric], window, start, last)
            sections.append(md)
            print("\n" + md + "\n")

    if {"price", "carbon"} <= set(all_predictions):
        table, n_days = simulate.simulate(all_predictions["price"], all_predictions["carbon"])
        md = simulate.to_markdown(table, n_days)
        sections.append(md)
        print("\n" + md + "\n")

    if sections:
        out = REPORTS_DIR / "scoreboard.md"
        out.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
        print(f"Saved {out} and the per-hour predictions as CSV in {REPORTS_DIR}/")


def cmd_forecast(model: str, metrics: list[str]) -> None:
    init_db()
    tomorrow = local_today() + timedelta(days=1)
    with SessionLocal() as session:
        for metric in metrics:
            rows = make_and_store_forecast(session, metric, model, tomorrow)
            unit = METRIC_UNITS[metric]
            if not rows:
                print(f"[SKIP] {metric}: not enough history to forecast {tomorrow} with {model}.")
                continue
            print(f"\n{metric.title()} forecast for {tomorrow} ({model}), {len(rows)} hours:")
            for r in rows:
                local = r["target_ts"].astimezone(ZoneInfo(LOCAL_TZ))
                print(f"  {local:%H:%M}  {r['value']:8.1f} {unit}")


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="gridpilot-forecast")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest")
    bt.add_argument("--days", type=int, default=365)
    bt.add_argument("--window", type=int, default=4, help="job length in hours for the window score")
    bt.add_argument("--metric", choices=["price", "carbon", "both"], default="both")
    bt.add_argument("--no-ml", action="store_true", help="skip the LightGBM model (faster)")

    fc = sub.add_parser("forecast")
    fc.add_argument("--model", choices=list(MODELS), default="profile_4w")
    fc.add_argument("--metric", choices=["price", "carbon", "both"], default="both")

    args = parser.parse_args()
    metrics = ["price", "carbon"] if args.metric == "both" else [args.metric]
    if args.command == "backtest":
        cmd_backtest(args.days, args.window, metrics, use_ml=not args.no_ml)
    else:
        cmd_forecast(args.model, metrics)


if __name__ == "__main__":
    main()
