#!/bin/sh
# docker-entrypoint.sh
# Runs at container start on Render (and locally if needed).
# Bootstraps the database then starts uvicorn.
set -e

db/render_bootstrap.sh

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
