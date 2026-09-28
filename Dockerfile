# Base image: python:3.11-slim
# In environments without Docker Hub access, build the base locally:
#   docker tag <existing-python-3.11-image> python-3.11-slim-local:latest
# then set PYTHON_BASE=python-3.11-slim-local in your .env.
ARG PYTHON_BASE=python:3.11-slim
FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System dependencies for psycopg2-binary and the bootstrap script.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq-dev postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy the full source first (setuptools needs the package directories
# to exist when building from pyproject.toml).
COPY . .

# Install all dependencies including dev extras (pytest, httpx, etc.).
# The package itself is installed in editable mode so imports resolve
# correctly from /app without needing to rebuild on every code change.
# Increased timeout and retries for environments with slow network.
RUN pip install --no-cache-dir --timeout=120 --retries=5 -e ".[dev]"

# Ensure the bootstrap scripts are executable.
RUN chmod +x /app/db/bootstrap_roles.sh /app/db/render_bootstrap.sh /app/docker-entrypoint.sh

# Default entrypoint: settlement-engine for local docker-compose.
# Overridden per-service in docker-compose.yml.
# On Render: docker-entrypoint.sh runs bootstrap then uvicorn app:app.
CMD ["/app/docker-entrypoint.sh"]
