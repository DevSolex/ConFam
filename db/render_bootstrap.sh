#!/usr/bin/env bash
# =============================================================================
# db/render_bootstrap.sh
#
# Bootstrap script for Render deployments where only ONE database user exists
# (the DB owner — no superuser access to create additional roles).
#
# Differences from bootstrap_roles.sh (local Docker setup):
#   - No CREATE ROLE — uses the single Render DB user for everything
#   - No ALTER ROLE BYPASSRLS — not available without superuser
#   - RLS policies in migration 011 are rewritten for Render (see migration 012)
#   - The append-only trigger on ledger_entries still holds for all users
#   - FORCE ROW LEVEL SECURITY still applies to the table owner
#
# What is preserved:
#   - Append-only enforcement on ledger_entries (BEFORE trigger — Rule 2)
#   - RLS on payout_accounts with FORCE (owner is still subject to policies)
#   - The ledger trigger as second-line defence
#
# What is weaker:
#   - Single DB user for both migrations and runtime (no app/migrator split)
#   - DATABASE_URL and ALEMBIC_DATABASE_URL point to the same user
#   - Documented as a known gap for the Render pilot deployment
#
# Required environment variables:
#   DATABASE_URL — Render-provided connection string (used for migrations too)
#
# =============================================================================

set -euo pipefail

echo "[render-bootstrap] Starting..."

# Wait for Postgres to be ready
python3 -c "
import os, time, psycopg2
url = os.environ['DATABASE_URL']
for i in range(30):
    try:
        conn = psycopg2.connect(url)
        conn.close()
        print('[render-bootstrap] Postgres is ready.')
        break
    except Exception as e:
        print(f'[render-bootstrap] Waiting for Postgres... ({i+1}/30)')
        time.sleep(2)
else:
    print('[render-bootstrap] ERROR: Postgres not ready after 60s')
    exit(1)
"

# Run Alembic migrations using the single Render DB user
# Render provides DATABASE_URL as postgresql:// — we must prefix it with
# +psycopg2 so SQLAlchemy uses psycopg2-binary (installed) not psycopg3 (not installed).
PSYCOPG2_URL="${DATABASE_URL/postgresql:\/\//postgresql+psycopg2:\/\/}"
export ALEMBIC_DATABASE_URL="$PSYCOPG2_URL"

echo "[render-bootstrap] Running Alembic migrations..."
alembic upgrade head

echo "[render-bootstrap] Done. Starting application..."
