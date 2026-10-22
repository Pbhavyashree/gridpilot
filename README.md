# GridPilot

**An energy-aware job scheduler for the German grid.** GridPilot forecasts day-ahead electricity prices and grid carbon intensity, runs flexible batch jobs in the cheapest or cleanest window before their deadline, and monitors its own health with anomaly detection.

> Status: **Phase 2 — baseline forecasts.** Scheduling and monitoring are next (see Roadmap).

## Forecast results

<!-- Paste the output of reports/scoreboard.md here after running the backtest. -->

_Run `python -m app.forecast.cli backtest --days 365` and paste `reports/scoreboard.md` here._

Every forecast is made as it would have been in real life: at 10:00 Berlin time the day before, using only data that was published by then. Prices for today are known (the day-ahead auction ran yesterday); carbon intensity is only known through yesterday.

**Regret** is the number that matters for scheduling: how much more an average hour of a 4-hour job cost in the window the forecast picked than in the truly cheapest window that day. `fixed_00:00` is the no-forecast reference, a plain cron job that always starts at midnight.

## What works today

- Hourly ingestion of German day-ahead prices (bidding zone DE-LU) and the public generation mix
- Automatic **fallback to aWATTar** when the primary source (Energy-Charts) is down, with every attempt logged
- Carbon-intensity **estimate** computed from the generation mix
- Native 15-minute data stored as-is, resampled to hourly in one place
- Idempotent upserts: re-running any day never creates duplicates
- Rate-limit aware: backs off on HTTP 429 and respects `Retry-After`; `coverage` finds gaps and `--only-missing` refills them
- Three baseline forecasts with a leak-free walk-forward backtest
- FastAPI endpoints: `/health`, `/prices`, `/forecast`, `/ingest/runs`
- 44 tests: parsing, fallback, idempotency, DST handling, no-future-data checks, scoring; CI on every push

## Run it locally

**Option A: Python only (SQLite, quickest)**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env

python -m app.ingest.cli check        # can we reach the APIs?
python -m app.ingest.cli once         # load the last 2 days + tomorrow
uvicorn app.main:app --reload         # then open http://localhost:8000/docs
```

**Option B: Docker (Postgres + API + hourly ingestor)**

```powershell
copy .env.example .env
docker compose up --build
```

Load history (about 2 years, takes a while), then check for and fill gaps:

```powershell
python -m app.ingest.cli backfill --days 730
python -m app.ingest.cli coverage --days 730
python -m app.ingest.cli backfill --days 730 --only-missing
```

(With Docker, prefix each with `docker compose exec ingestor`.)

**Forecasts**

```powershell
python -m app.forecast.cli backtest --days 365   # scoreboard -> reports/scoreboard.md
python -m app.forecast.cli forecast              # tomorrow's forecast, saved; see GET /forecast
```

## Project layout

```
app/
  main.py            FastAPI app
  config.py          settings from environment
  db.py              engine, sessions, upsert helper
  models.py          tables: prices, generation, carbon_intensity, ingest_runs
  queries.py         hourly resampling
  ingest/
    energy_charts.py primary source client
    awattar.py       fallback price client
    carbon.py        carbon-intensity estimate
    pipeline.py      fetch -> transform -> upsert, fallback, run log
    coverage.py      finds incomplete days
    cli.py           check | once | backfill | coverage | worker
  forecast/
    baselines.py     naive_day, naive_week, profile_4w + the "what was known when" rules
    backtest.py      walk-forward backtest, MAE and window scores
    service.py       load data, make and store forecasts
    cli.py           backtest | forecast
tests/
```

## Roadmap

- [x] Phase 0 — project setup, Docker, CI
- [x] Phase 1 — data ingestion with fallback
- [x] Phase 2 — baseline forecasts and walk-forward backtest
- [ ] Phase 3 — job API and deadline-aware scheduler
- [ ] Phase 4 — LightGBM forecast and savings simulation
- [ ] Phase 5 — Prometheus, Grafana, anomaly detection
- [ ] Phase 6 — deployment

## Data and attribution

- Prices and generation: **Energy-Charts / Fraunhofer ISE** ([api.energy-charts.info](https://api.energy-charts.info/)), used for non-commercial purposes with attribution.
- Fallback prices: [aWATTar API](https://www.awattar.de/services/api), within its fair-use limits.
- Carbon intensity is an **estimate** based on approximate life-cycle emission factors (mostly IPCC AR5 medians); see `app/ingest/carbon.py`.

## License

MIT
