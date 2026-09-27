#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Deploy or redeploy ConFam on the EC2 instance
#
# Usage from local machine:
#   ./deploy.sh ubuntu@<instance-ip>
#
# Usage directly on the instance:
#   ./deploy.sh
#
# Requires: SSH key auth set up, .env present on the instance.
# =============================================================================

set -euo pipefail

REMOTE="${1:-}"
APP_DIR="/home/ubuntu/ConFam"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

run() {
  if [ -n "$REMOTE" ]; then
    ssh "$REMOTE" "cd $APP_DIR && $*"
  else
    eval "$*"
  fi
}

echo "[deploy] Pulling latest code from main..."
run "git pull origin main"

echo "[deploy] Rebuilding images..."
run "$COMPOSE build"

echo "[deploy] Restarting stack..."
run "$COMPOSE up -d"

echo "[deploy] Waiting for services to start..."
sleep 8

echo "[deploy] Health checks..."
if [ -n "$REMOTE" ]; then
  HOSTNAME=$(ssh "$REMOTE" "grep PUBLIC_HOSTNAME $APP_DIR/.env | cut -d= -f2 | tr -d '\r'")
  curl -sf "https://$HOSTNAME/health" && echo "  ✓ checkout healthy" || echo "  ✗ checkout failed"
else
  curl -sf http://localhost:8001/health && echo "  ✓ checkout" || echo "  ✗ checkout"
  curl -sf http://localhost:8002/health && echo "  ✓ messaging" || echo "  ✗ messaging"
fi

echo "[deploy] Done."
