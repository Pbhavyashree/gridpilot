"""Central configuration, read from environment variables (or a local .env file)."""

import os

from dotenv import load_dotenv

load_dotenv()

# SQLite by default so you can run everything locally without Docker.
# docker-compose.yml overrides this with the Postgres URL.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gridpilot.db")

# Bidding zone for Germany + Luxembourg on the day-ahead market.
BIDDING_ZONE = os.getenv("BIDDING_ZONE", "DE-LU")
COUNTRY = os.getenv("COUNTRY", "de")

ENERGY_CHARTS_BASE_URL = os.getenv("ENERGY_CHARTS_BASE_URL", "https://api.energy-charts.info")
AWATTAR_BASE_URL = os.getenv("AWATTAR_BASE_URL", "https://api.awattar.de")
OPEN_METEO_BASE_URL = os.getenv("OPEN_METEO_BASE_URL", "https://previous-runs-api.open-meteo.com")
# How old the weather forecasts we use are, in days. 2 is always available at 10:00 the day before.
WEATHER_LEAD_DAYS = int(os.getenv("WEATHER_LEAD_DAYS", "2"))

HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))

# Scheduler
# lgbm won the Phase 4 backtest. Until a model is trained it falls back to profile_4w automatically.
FORECAST_MODEL = os.getenv("FORECAST_MODEL", "lgbm")
MODEL_DIR = os.getenv("MODEL_DIR", "models")
MAX_HORIZON_HOURS = int(os.getenv("MAX_HORIZON_HOURS", "72"))  # forecasts get poor further out
MAX_DURATION_HOURS = int(os.getenv("MAX_DURATION_HOURS", "24"))
REPLAN_FREEZE_MINUTES = int(os.getenv("REPLAN_FREEZE_MINUTES", "15"))  # don't move jobs about to start

# If set, POST/DELETE on /jobs need the header  X-API-Key: <value>.  Leave empty for local dev.
API_KEY = os.getenv("API_KEY", "")
