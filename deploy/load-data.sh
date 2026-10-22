#!/usr/bin/env bash
#
# Load GridPilot's history into the production database and train the models.
# Run once after setup.sh. Safe to re-run: every load is an idempotent upsert.
#
#   bash ~/gridpilot/deploy/load-data.sh
#
# Takes about 15 minutes, mostly waiting politely on the Energy-Charts rate limit.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$DEPLOY_DIR/docker-compose.prod.yml" --env-file "$DEPLOY_DIR/.env")
run() { "${COMPOSE[@]}" exec -T api "$@"; }
say() { printf '\n=== %s ===\n' "$1"; }

say "1/5  Prices and generation, last 2 years"
run python -m app.ingest.cli backfill --days 730

say "2/5  Filling any gaps left by rate limits"
run python -m app.ingest.cli backfill --days 730 --only-missing
run python -m app.ingest.cli coverage --days 730

say "3/5  Weather forecasts"
run python -m app.ingest.cli weather --days 730

say "4/5  Training the lgbm models"
run python -m app.forecast.cli train

say "5/5  First health check"
run python -m app.monitor.cli check

cat <<'EOF'

Data loaded and models trained. From here everything runs by itself:
  - ingestor:   new prices, generation and weather every hour
  - scheduler:  runs jobs, replans hourly, health checks every 15 min,
                stores the day-ahead forecast daily (for drift detection),
                and schedules its own model retraining every Saturday

If `coverage` above still lists gaps, run step 2 again later.
EOF
