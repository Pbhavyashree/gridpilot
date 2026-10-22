# GridPilot

**An energy-aware job scheduler for the German grid.** GridPilot forecasts day-ahead electricity prices and grid carbon intensity, runs flexible batch jobs in the cheapest or cleanest window before their deadline, and monitors its own health with anomaly detection.

**Result:** replaying a year of real German grid data (Oct 2025 to Oct 2026), GridPilot's weather-aware scheduler cut the electricity cost of a set of everyday batch jobs by **50%** and their emissions by **42%** compared with a nightly cron job, capturing **97% / 96%** of what perfect foresight could have achieved.

> Status: **Phase 4b — the live scheduler plans with the weather-aware LightGBM model.** Monitoring is next (see Roadmap).

## How scheduling works

Submit a job with a deadline; GridPilot answers with when it will run:

```http
POST /jobs
{"name": "Nightly report refresh", "task": "demo", "duration_hours": 2,
 "deadline": "2026-10-24T08:00", "objective": "cost"}
```

1. **Build a curve** for every hour between now and the deadline. Real data wins: day-ahead prices are *published* for today (and tomorrow after ~13:00), carbon is *measured* for past hours. Everything else comes from the weather-aware `lgbm` forecast, the Phase 4 winner. If no trained model exists yet, those hours fall back to `profile_4w`, and `/explain` says so.
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
| lgbm | 20.8 | 50% | 1.9 |
| profile_4w | 32.9 | 44% | 3.7 |
| naive_day | 30.4 | 41% | 5.4 |
| naive_week | 38.3 | 37% | 5.7 |
| fixed_11:00 (best in hindsight) | – | 25% | 10.6 |
| fixed_00:00 | – | 5% | 60.6 |

_362 complete days scored. Regret = how much worse the chosen window was than the best one._

**Carbon forecast backtest** (2025-10-22 to 2026-10-21, 4-hour window)

| Model | MAE (g/kWh) | Best window found | Regret (g/kWh) |
| --- | --- | --- | --- |
| lgbm | 46.4 | 41% | 7.0 |
| fixed_11:00 (best in hindsight) | – | 42% | 12.4 |
| profile_4w | 96.5 | 32% | 15.0 |
| naive_day | 100.9 | 36% | 16.5 |
| naive_week | 117.0 | 29% | 21.8 |
| fixed_00:00 | – | 7% | 177.3 |

_362 complete days scored. Regret = how much worse the chosen window was than the best one._

**One-year savings simulation** (361 days, every job once per day)

Jobs: Nightly data refresh (2 h, 1 kW, cost); Model retraining (4 h, 5 kW, carbon); Backups (1 h, 0.5 kW, blend).

| Strategy | Cost (EUR) | CO₂ (kg) | Cost saved vs cron | CO₂ saved vs cron | Share of possible saving (cost / CO₂) |
| --- | --- | --- | --- | --- | --- |
| cron 00:00 | 886 | 3,291 | 0.0% | 0.0% | – |
| best fixed hour (hindsight) | 471 | 1,951 | 46.9% | 40.7% | 90% / 93% |
| naive_day | 459 | 1,986 | 48.2% | 39.6% | 93% / 91% |
| naive_week | 469 | 2,026 | 47.0% | 38.4% | 91% / 88% |
| profile_4w | 456 | 1,973 | 48.5% | 40.0% | 94% / 92% |
| lgbm | 442 | 1,908 | 50.2% | 42.0% | 97% / 96% |
| perfect foresight | 426 | 1,852 | 51.9% | 43.7% | 100% / 100% |

_Each job is charged at the real prices and carbon intensity of the hours it actually ran. Forecast strategies use the same placement code as the live scheduler._

**Findings**

1. **Weather was the missing signal.** With 2-day-old weather forecasts as inputs, LightGBM halves the carbon forecast error (MAE 46 vs 97 g/kWh) and, for the first time, beats the fixed 11:00 rule on carbon: regret 7.0 vs 12.4 g/kWh, 44% lower, even though that rule was picked with hindsight. Weather features carry about 60% of the carbon model's decisions and about 40% of the price model's.
2. **For price**, `lgbm` cuts regret to 1.9 EUR/MWh: half of `profile_4w`'s 3.7, and 82% below the best fixed hour.
3. **Lowest error isn't automatically the best scheduler.** Among the baselines, `naive_day` has the best price MAE, but `profile_4w` picks better windows. The scheduler needs the *shape* of the day.
4. **Most of the saving comes from simply not running at midnight.** In the one-year simulation even the best fixed hour saves 47% of cost and 41% of CO₂. Forecasting adds the last stretch: `lgbm` captures 97% / 96% of the possible saving (vs 90% / 93%), closing about two-thirds of the remaining cost gap to perfect foresight and about 40% of the CO₂ gap.

These numbers are conservative in two ways: the weather forecasts used are about 2 days old (live they'd be fresher), and the simulation plans on price forecasts even though real day-ahead prices are published around 13:00 and the live scheduler uses them.

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
- LightGBM forecaster with weather features (wind, solar radiation, temperature), retrained monthly in a walk-forward backtest
- Honest weather inputs: archived **2-day-old weather forecasts** from Open-Meteo, never the real historical weather, so the backtest can't see the future
- One-year savings simulation that replays realistic jobs through the live placement code
- The live scheduler uses `lgbm` (retrained by a scheduled job, so GridPilot picks a low-carbon window for its own retraining) and falls back safely when no model exists; `GET /models` shows what's trained
- FastAPI endpoints: `/jobs`, `/jobs/{id}/explain`, `/tasks`, `/prices`, `/forecast`, `/ingest/runs`, `/health`
- 84 tests: parsing, fallback, DST handling, no-future-data checks, placement edge cases, job lifecycle, API; CI on every push

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
python -m app.ingest.cli weather --days 730      # weather forecasts for the ML model (once)
python -m app.forecast.cli backtest --days 365   # baselines + lgbm + savings simulation -> reports/scoreboard.md
python -m app.forecast.cli train                 # train the live lgbm models -> models/ (repeat weekly)
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
    weather.py       Open-Meteo archived weather forecasts
    coverage.py      finds incomplete days
    cli.py           check | once | backfill | coverage | worker
  forecast/
    baselines.py     naive_day, naive_week, profile_4w + the "what was known when" rules
    backtest.py      walk-forward backtest, MAE and window scores
    ml.py            LightGBM: leak-free features, monthly walk-forward retraining
    simulate.py      one-year savings simulation
    live.py          train, save, load and predict with lgbm; falls back to profile_4w
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
- [x] Phase 4 — LightGBM forecast with weather, savings simulation
- [x] Phase 4b — use lgbm in the live scheduler
- [ ] Phase 5 — Prometheus, Grafana, anomaly detection
- [ ] Phase 6 — deployment

## Data and attribution

- Prices and generation: **Energy-Charts / Fraunhofer ISE** ([api.energy-charts.info](https://api.energy-charts.info/)), used for non-commercial purposes with attribution.
- Fallback prices: [aWATTar API](https://www.awattar.de/services/api), within its fair-use limits.
- Weather forecasts: [Open-Meteo Previous Model Runs API](https://open-meteo.com/en/docs/previous-runs-api), CC BY 4.0.
- Carbon intensity is an **estimate** based on approximate life-cycle emission factors (mostly IPCC AR5 medians); see `app/ingest/carbon.py`.

## License

MIT
