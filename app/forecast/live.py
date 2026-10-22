"""Using the LightGBM model for real: train, save, load, predict, with a safe fallback.

    train_and_save(session, metric)    fit on everything known right now, save to MODEL_DIR
    forecast_values(session, metric, model, day)
        -> (24 hourly values, the model actually used)

If `lgbm` is asked for but there's no trained model yet, or it can't produce a value
for some hour, those hours fall back to `profile_4w`, and the fallback is reported.
The scheduler never stops working just because a model file is missing.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from app import config
from app.forecast import ml
from app.forecast.baselines import LOCAL_TZ, MODELS, forecast_day, known_through

log = logging.getLogger(__name__)

FALLBACK_MODEL = "profile_4w"
_cache: dict[str, tuple[float, object, dict]] = {}  # path -> (mtime, booster, metadata)


def model_paths(metric: str) -> tuple[Path, Path]:
    directory = Path(config.MODEL_DIR)
    return directory / f"lgbm_{metric}.txt", directory / f"lgbm_{metric}.json"


def _local_today() -> date:
    return datetime.now(ZoneInfo(LOCAL_TZ)).date()


# ---------- training ----------


def train_and_save(session, metric: str, today: date | None = None) -> dict:
    """Fit on all data known as of today and save the model. Returns its metadata."""
    import lightgbm as lgb

    from app.forecast.service import load_matrix

    today = today or _local_today()
    cutoff = known_through(metric, today)
    matrix = load_matrix(session, metric, date(2015, 1, 1), cutoff)
    if matrix.empty:
        raise ValueError(f"no {metric} data to train on")

    first = matrix.index.min() + timedelta(days=35)
    weather = ml.load_weather(session, matrix.index.min(), cutoff)
    features = ml.build_features(matrix, metric, ml.weather_matrices(weather), first, cutoff)
    train = features[features["y"].notna()]
    days = train["target_day"].nunique()
    if days < ml.MIN_TRAIN_DAYS:
        raise ValueError(f"only {days} days of {metric} history; need {ml.MIN_TRAIN_DAYS}")

    cols = ml.feature_columns(features)
    booster = lgb.train(ml.LGBM_PARAMS, lgb.Dataset(train[cols], label=train["y"]), num_boost_round=ml.NUM_BOOST_ROUND)

    model_file, meta_file = model_paths(metric)
    model_file.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(model_file))
    meta = {
        "metric": metric,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trained_through": cutoff.isoformat(),
        "train_days": int(days),
        "first_day": train["target_day"].min().isoformat(),
        "uses_weather": not weather.empty,
        "features": cols,
    }
    meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    _cache.pop(str(model_file), None)
    log.info("Trained lgbm for %s on %d days (through %s)", metric, days, cutoff)
    return meta


# ---------- loading and predicting ----------


def load_model(metric: str):
    """(booster, metadata), or (None, None) if no model has been trained yet. Reloads when the file changes."""
    import lightgbm as lgb

    model_file, meta_file = model_paths(metric)
    if not model_file.exists() or not meta_file.exists():
        return None, None
    mtime = model_file.stat().st_mtime
    cached = _cache.get(str(model_file))
    if cached and cached[0] == mtime:
        return cached[1], cached[2]
    booster = lgb.Booster(model_file=str(model_file))
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    _cache[str(model_file)] = (mtime, booster, meta)
    return booster, meta


def model_info() -> list[dict]:
    out = []
    for metric in ("price", "carbon"):
        _, meta = load_model(metric)
        if meta:
            out.append({k: v for k, v in meta.items() if k != "features"} | {"n_features": len(meta["features"])})
        else:
            out.append({"metric": metric, "trained_at": None, "note": "not trained yet; run: python -m app.forecast.cli train"})
    return out


def predict_day(session, metric: str, day: date) -> np.ndarray | None:
    """lgbm forecast for one Berlin day, or None if no model exists."""
    from app.forecast.service import load_matrix

    booster, meta = load_model(metric)
    if booster is None:
        return None
    history_start = day - timedelta(days=40)
    matrix = load_matrix(session, metric, history_start, day)
    weather = ml.load_weather(session, history_start, day)
    features = ml.build_features(matrix, metric, ml.weather_matrices(weather), day, day)
    # Same columns, same order as at training time (missing ones become NaN, LightGBM copes).
    X = features.reindex(columns=meta["features"])
    values = np.full(24, np.nan)
    values[features["hour"].to_numpy()] = booster.predict(X)
    return values


def forecast_values(session, metric: str, model: str, day: date, matrix=None) -> tuple[np.ndarray, str]:
    """24 hourly values for `day` and the name of the model that produced them."""
    from app.forecast.service import load_matrix

    if matrix is None:
        matrix = load_matrix(session, metric, day - timedelta(days=35), day)
    if model in MODELS:
        return forecast_day(matrix, metric, day, model), model
    if model != ml.MODEL_NAME:
        raise ValueError(f"unknown model: {model}")

    values = predict_day(session, metric, day)
    fallback = forecast_day(matrix, metric, day, FALLBACK_MODEL)
    if values is None:
        return fallback, f"{FALLBACK_MODEL} (fallback: no trained lgbm)"
    missing = np.isnan(values)
    if missing.any():
        values = np.where(missing, fallback, values)
        return values, f"{ml.MODEL_NAME} ({int(missing.sum())}h from {FALLBACK_MODEL})"
    return values, ml.MODEL_NAME
