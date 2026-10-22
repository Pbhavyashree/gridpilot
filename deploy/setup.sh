#!/usr/bin/env bash
#
# One-time GridPilot setup on the energy-hub server (Ubuntu 24.04, Oracle Ampere).
# Safe to re-run: every step checks before it acts.
#
#   git clone https://github.com/Pbhavyashree/gridpilot.git ~/gridpilot
#   bash ~/gridpilot/deploy/setup.sh
#
# What it does:
#   1. Creates deploy/.env with random secrets (only if it doesn't exist yet)
#   2. Builds and starts GridPilot (no published ports; bahnwatch keeps port 8000)
#   3. Adds a gridpilot site to energy-hub's Caddyfile, VALIDATES it, and reloads
#      Caddy without a restart. If validation fails, the old Caddyfile is restored,
#      so energy-hub and bahnwatch are never affected.
#
# It does not load data; run load-data.sh afterwards.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CADDYFILE="${CADDYFILE:-$HOME/energy-hub/deploy/Caddyfile}"
CADDY_CONTAINER="${CADDY_CONTAINER:-deploy-caddy-1}"
EDGE_NETWORK="deploy_default"
COMPOSE=(docker compose -f "$DEPLOY_DIR/docker-compose.prod.yml" --env-file "$DEPLOY_DIR/.env")

say() { printf '\n=== %s ===\n' "$1"; }
die() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------
# 0. Sanity checks
# ---------------------------------------------------------------
[ "$(uname -s)" = "Linux" ] || die "run this on the server (over ssh), not on your laptop."
command -v docker >/dev/null || die "Docker isn't installed."
docker network inspect "$EDGE_NETWORK" >/dev/null 2>&1 \
    || die "network '$EDGE_NETWORK' not found. Is energy-hub running? (docker ps)"
docker inspect "$CADDY_CONTAINER" >/dev/null 2>&1 \
    || die "Caddy container '$CADDY_CONTAINER' not found. Set CADDY_CONTAINER=... and re-run."
[ -f "$CADDYFILE" ] || die "Caddyfile not found at $CADDYFILE. Set CADDYFILE=... and re-run."

# ---------------------------------------------------------------
# 1. Secrets
# ---------------------------------------------------------------
cd "$DEPLOY_DIR"
if [ -f .env ]; then
    say "Keeping existing deploy/.env"
else
    say "Creating deploy/.env with random secrets"
    cp .env.example .env
    chmod 600 .env
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env
    sed -i "s|^API_KEY=.*|API_KEY=$(openssl rand -hex 24)|" .env
    sed -i "s|^GRAFANA_ADMIN_PASSWORD=.*|GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 12)|" .env
    NEW_SECRETS=1
fi
# shellcheck disable=SC1091
DOMAIN="$(grep -E '^GRIDPILOT_DOMAIN=' .env | cut -d= -f2-)"
DOMAIN="${DOMAIN:-gridpilot.duckdns.org}"

# ---------------------------------------------------------------
# 2. Build and start
# ---------------------------------------------------------------
say "Building and starting GridPilot (the first build takes a few minutes on ARM)"
"${COMPOSE[@]}" up -d --build

say "Waiting for the API to become healthy"
for _ in $(seq 1 40); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$("${COMPOSE[@]}" ps -q api)" 2>/dev/null || true)"
    [ "$status" = "healthy" ] && break
    sleep 3
done
[ "$status" = "healthy" ] || die "API didn't become healthy. Look at: ${COMPOSE[*]} logs api"
echo "API is healthy."

# ---------------------------------------------------------------
# 3. Caddy route (validated, with automatic rollback)
# ---------------------------------------------------------------
if grep -q "^$DOMAIN {" "$CADDYFILE"; then
    say "Caddyfile already has a $DOMAIN site; leaving it alone"
else
    say "Adding $DOMAIN to energy-hub's Caddyfile"
    BACKUP="$CADDYFILE.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$CADDYFILE" "$BACKUP"
    echo "Backup: $BACKUP"

    # Append in place (same file, same inode), so Caddy's bind mount sees the change.
    sed "s|__GRIDPILOT_DOMAIN__|$DOMAIN|" "$DEPLOY_DIR/Caddyfile.gridpilot" >> "$CADDYFILE"

    # Where the Caddyfile is mounted inside the container.
    SOURCE="$(realpath "$CADDYFILE")"
    TARGET="$(docker inspect "$CADDY_CONTAINER" \
        --format "{{range .Mounts}}{{if eq .Source \"$SOURCE\"}}{{.Destination}}{{end}}{{end}}")"
    TARGET="${TARGET:-/etc/caddy/Caddyfile}"

    if docker exec "$CADDY_CONTAINER" caddy validate --config "$TARGET" --adapter caddyfile >/dev/null 2>&1; then
        docker exec "$CADDY_CONTAINER" caddy reload --config "$TARGET" --adapter caddyfile
        echo "Caddy reloaded (no restart; energy-hub and bahnwatch kept running)."
    else
        cat "$BACKUP" > "$CADDYFILE"  # restore in place
        docker exec "$CADDY_CONTAINER" caddy validate --config "$TARGET" --adapter caddyfile || true
        die "the new Caddy config didn't validate; the original was restored. Nothing changed."
    fi
fi

# ---------------------------------------------------------------
# Done
# ---------------------------------------------------------------
say "GridPilot is running"
"${COMPOSE[@]}" ps --format "table {{.Service}}\t{{.Status}}"

cat <<EOF

Next:
  1. Load two years of data and train the models (about 15 minutes):
       bash $DEPLOY_DIR/load-data.sh

  2. Then open (the HTTPS certificate is issued on the first visit; give it a minute):
       https://$DOMAIN/docs          the API
       https://$DOMAIN/grafana/      the dashboard
       https://$DOMAIN/incidents     what GridPilot noticed about itself

  3. Commit the Caddyfile change in the energy-hub repo, so git matches the server:
       cd ~/energy-hub && git diff deploy/Caddyfile
EOF

if [ "${NEW_SECRETS:-0}" = "1" ]; then
    cat <<EOF

Your secrets are in $DEPLOY_DIR/.env (readable only by you). Save these two somewhere safe:
  API key (for submitting jobs):  $(grep -E '^API_KEY=' .env | cut -d= -f2-)
  Grafana admin password:         $(grep -E '^GRAFANA_ADMIN_PASSWORD=' .env | cut -d= -f2-)
EOF
fi
