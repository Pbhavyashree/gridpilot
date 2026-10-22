# GridPilot

**An energy-aware job scheduler for the German grid.** GridPilot forecasts day-ahead electricity prices and grid carbon intensity, runs flexible batch jobs in the cheapest or cleanest window before their deadline, and monitors its own health with anomaly detection.

> Status: **Phase 3 — the scheduler works.** Better forecasts and monitoring are next (see Roadmap).

## How scheduling works

Submit a job with a deadline; GridPilot answers with when it will run:

```http
POST /jobs
{"name": "Nightly report refresh", "task": "demo", "duration_hours": 2,
 "deadline": "2026-10-24T08:00", "objective": "cost"}
```

1. **Build a curve** for every hour between now and the deadline. Real data wins: day-ahead prices are *published* for today (and tomorrow after ~13:00), carbon is *measured* for past hours. Everything else comes from the `profile_4w` forecast, the Phase 2 winner.
2. **Slide the job's window** across every possible start and pick the lowest expected cost, CO₂, or a weighted blend of both.
3. **Replan hourly**, because forecasts improve and new prices get published. Jobs about to start within 15 minutes are never moved.
4. **Run** at the planned time. Jobs can only run whitelisted tasks (`GET /tasks`), never arbitrary commands.
5. **Settle**: once the window has passed, compute what the job *really* cost from measured data, next to what was expected.

`GET /jobs/{id}/explain` shows the curve it planned against (with which hours were real data and which were forecasts), what running immediately would have cost, and the savings.

## Forecast results

<!-- Regenerate with: python -m app.forecast.cli backtest --days 365, then paste reports/scoreboard.md here. -->

**Price forecast backtest** (2025-10-22 to 2026-10-21, 4-hour window)

| Model | MAE (EUR/MWh) | Best window found | Regret (EUR/MWh) |
| --- | --- | --- | --- |
| profile_4w | 32.9 | 44% | 3.7 |
| naive_day | 30.4 | 41% | 5.4 |
| naive_week | 38.3 | 37% | 5.7 |
| fixed_11:00 (best in hindsight) | – | 25% | 10.6 |
| fixed_00:00 | – | 5% | 60.6 |

_362 complete days scored. Regret = how much worse the chosen window was than the best one._

**Carbon forecast backtest** (2025-10-22 to 2026-10-21, 4-hour window)

| Model | MAE (g/kWh) | Best window found | Regret (g/kWh) |
| --- | --- | --- | --- |
| fixed_11:00 (best in hindsight) | – | 42% | 12.4 |
| profile_4w | 96.5 | 32% | 15.0 |
| naive_day | 100.9 | 36% | 16.5 |
| naive_week | 117.0 | 29% | 21.8 |
| fixed_00:00 | – | 7% | 177.3 |

_362 complete days scored. Regret = how much worse the chosen window was than the best one._

**Findings.** (1) The model with the lowest error isn't the best scheduler: `naive_day` has the best price MAE, but `profile_4w` picks better windows. (2) For **price**, forecast-based scheduling beats even the best fixed start hour chosen in hindsight (regret 3.7 vs 10.6 EUR/MWh, about 65% lower). (3) For **carbon**, it doesn't yet: a fixed 11:00 start (regret 12.4 g/kWh) beats all baselines (best 15.0), because carbon intensity is driven by wind, which doesn't repeat weekly. Beating that rule is the target for the weather-aware model in Phase 4.

Every forecast is made as it would have been in real life: at 10:00 Berlin time the day before, using only data that was published by then. Prices for today are known (the day-ahead auction ran yesterday); carbon intensity is only known through yesterday.

**Regret** is the number that matters for scheduling: how much more an average hour of a 4-hour job cost in the window the forecast picked than in the truly cheapest window that day. There are two no-forecast references: `fixed_00:00`, a plain cron job that always starts at midnight, and the **best fixed start hour in hindsight**, which is chosen after seeing the whole year and is therefore a deliberately tough comparison.

## What works today

- Hourly ingestion of German day-ahead prices (bidding zone DE-LU) and the public generation mix
- Automatic **fallback to aWATTar** when the primary source (Energy-Charts) is down, with every attempt logged
- Carbon-intensity **estimate** computed from the generation mix
- Native 15-minute data stored as-is, resampled to hourly in one place
- Idempotent upserts: re-running any day never creates duplicates
- Rate-limit aware: backs off on HTTP 429 and respects `Retry-After`; `coverage` finds gaps and `--only-missing` refills them
- Three baseline forecasts with a leak-free walk-forward backtest
- Deadline-aware job scheduler: cost, carbon or blended objective, hourly replanning, settlement against real data
- FastAPI endpoints: `/jobs`, `/jobs/{id}/explain`, `/tasks`, `/prices`, `/forecast`, `/ingest/runs`, `/health`
- 71 tests: parsing, fallback, DST handling, no-future-data checks, placement edge cases, job lifecycle, API; CI on every push

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

**Scheduler**

```powershell
python -m app.scheduler.cli demo     # submit 5 example jobs and print their plans
python -m app.scheduler.cli list     # all jobs and their status
python -m app.scheduler.cli worker   # keep running: start due jobs, replan and settle hourly
```

In Docker the `scheduler` service runs the worker for you.

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
  scheduler/
    curve.py         real data where it exists, forecasts where it doesn't
    placement.py     pure function: curve + job -> best window
    service.py       create, replan, run, settle
    tasks.py         the whitelist of things a job may run
    cli.py           demo | list | tick | worker
  api/
    jobs.py          /jobs, /jobs/{id}/explain, /tasks
tests/
```

## Roadmap

- [x] Phase 0 — project setup, Docker, CI
- [x] Phase 1 — data ingestion with fallback
- [x] Phase 2 — baseline forecasts and walk-forward backtest
- [x] Phase 3 — job API and deadline-aware scheduler
- [ ] Phase 3b — capacity limits (at most N jobs at once)
- [ ] Phase 4 — LightGBM forecast and savings simulation
- [ ] Phase 5 — Prometheus, Grafana, anomaly detection
- [ ] Phase 6 — deployment

## Data and attribution

- Prices and generation: **Energy-Charts / Fraunhofer ISE** ([api.energy-charts.info](https://api.energy-charts.info/)), used for non-commercial purposes with attribution.
- Fallback prices: [aWATTar API](https://www.awattar.de/services/api), within its fair-use limits.
- Carbon intensity is an **estimate** based on approximate life-cycle emission factors (mostly IPCC AR5 medians); see `app/ingest/carbon.py`.

## License

MIT
