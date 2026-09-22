#!/usr/bin/env bash
# =============================================================================
# db/bootstrap_roles.sh
#
# Creates the confam_app and confam_migrator Postgres roles if they don't
# already exist, then runs Alembic migrations as confam_migrator.
#
# Called by:
#   - docker compose: the `migrate` service (entrypoint)
#   - CI: the "Create confam_app role" step in each database job
#     (CI uses a simpler inline psql command — see .github/workflows/ci.yml —
#     but this script is the authoritative reference for what bootstrap does)
#
# Required environment variables:
#   POSTGRES_HOST      — hostname of the Postgres instance
#   POSTGRES_PORT      — port (default: 5432)
#   POSTGRES_DB        — database name
#   POSTGRES_SUPERUSER — superuser role name (default: postgres)
#   POSTGRES_SUPERUSER_PASSWORD
#   CONFAM_APP_PASSWORD     — password to set on confam_app
#   CONFAM_MIGRATOR_PASSWORD — password to set on confam_migrator
#   ALEMBIC_DATABASE_URL    — passed through to alembic upgrade head
#
# The script is idempotent: running it twice produces the same result.
# =============================================================================

set -euo pipefail

POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-confam}"
POSTGRES_SUPERUSER="${POSTGRES_SUPERUSER:-postgres}"

echo "[bootstrap] Waiting for Postgres at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
until PGPASSWORD="${POSTGRES_SUPERUSER_PASSWORD}" \
  psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" \
       -U "${POSTGRES_SUPERUSER}" -d postgres \
       -c "SELECT 1" > /dev/null 2>&1; do
  echo "[bootstrap] Postgres not ready — retrying in 1s..."
  sleep 1
done
echo "[bootstrap] Postgres is ready."

echo "[bootstrap] Creating roles (idempotent)..."
PGPASSWORD="${POSTGRES_SUPERUSER_PASSWORD}" \
  psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" \
       -U "${POSTGRES_SUPERUSER}" -d postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'confam_migrator') THEN
    CREATE ROLE confam_migrator WITH LOGIN PASSWORD '${CONFAM_MIGRATOR_PASSWORD}';
    RAISE NOTICE 'Created role confam_migrator';
  ELSE
    ALTER ROLE confam_migrator WITH PASSWORD '${CONFAM_MIGRATOR_PASSWORD}';
    RAISE NOTICE 'Role confam_migrator already exists — password updated';
  END IF;

  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'confam_app') THEN
    CREATE ROLE confam_app WITH LOGIN PASSWORD '${CONFAM_APP_PASSWORD}';
    RAISE NOTICE 'Created role confam_app';
  ELSE
    ALTER ROLE confam_app WITH PASSWORD '${CONFAM_APP_PASSWORD}';
    RAISE NOTICE 'Role confam_app already exists — password updated';
  END IF;
END
\$\$;

-- Grant confam_migrator ownership of the database so it can run DDL.
GRANT ALL PRIVILEGES ON DATABASE "${POSTGRES_DB}" TO confam_migrator;
SQL

# Grant CREATE on the public schema *inside the confam database* — Postgres 15+
# revokes this by default even for database owners.
PGPASSWORD="${POSTGRES_SUPERUSER_PASSWORD}" \
  psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" \
       -U "${POSTGRES_SUPERUSER}" -d "${POSTGRES_DB}" \
       -c "GRANT CREATE ON SCHEMA public TO confam_migrator;"

echo "[bootstrap] Running Alembic migrations as confam_migrator..."
alembic upgrade head

echo "[bootstrap] Done. Schema is up to date."
