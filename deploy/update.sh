#!/usr/bin/env bash
#
# Deploy the latest code from GitHub. Data, models and secrets are kept.
#
#   bash ~/gridpilot/deploy/update.sh

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$DEPLOY_DIR/docker-compose.prod.yml" --env-file "$DEPLOY_DIR/.env")

cd "$DEPLOY_DIR/.."
echo "=== Pulling latest code ==="
git pull --ff-only

echo "=== Rebuilding and restarting changed services ==="
"${COMPOSE[@]}" up -d --build

echo "=== Removing old images ==="
docker image prune -f >/dev/null

"${COMPOSE[@]}" ps --format "table {{.Service}}\t{{.Status}}"
