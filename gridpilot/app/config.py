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

HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
