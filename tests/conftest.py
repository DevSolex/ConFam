"""
Shared pytest fixtures for ConFam tests.

Database fixtures connect using environment variables set by CI:
  TEST_DATABASE_URL        — used by confam_app (privilege-restricted role)
  ALEMBIC_DATABASE_URL     — used by confam_migrator (migration/admin role)

See db/migrations/007_create_roles_and_grants.sql for role definitions.
See OPEN_QUESTIONS.md OQ-018 (resolved) for role design rationale.
"""

import os
import pytest
import psycopg2
import confam.db as _confam_db


@pytest.fixture(autouse=True)
def reset_db_pool_on_database_url_change(monkeypatch):
    """Reset connection pool after each test to prevent URL contamination."""
    yield
    _confam_db.close_pool()


@pytest.fixture(scope="session")
def app_db_conn():
    """
    A database connection as confam_app — the restricted application role.
    Used for privilege-level rejection tests (ledger_append_only marker).

    The confam_app role has no UPDATE/DELETE grant on ledger_entries.
    Attempting either should fail at the privilege level (PG error code 42501),
    not merely at the trigger level.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set — skipping database tests")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def migrator_db_conn():
    """
    A database connection as confam_migrator — the privileged migration role.
    Used for trigger-level rejection tests (confam_migrator CAN attempt
    UPDATE/DELETE on ledger_entries — the trigger is the backstop for this role).
    """
    url = os.environ.get("ALEMBIC_DATABASE_URL", "")
    # ALEMBIC_DATABASE_URL uses SQLAlchemy driver prefix (postgresql+psycopg2://).
    # psycopg2.connect() needs a plain postgresql:// DSN — strip the driver suffix.
    url = url.replace("postgresql+psycopg2://", "postgresql://")
    if not url:
        pytest.skip("ALEMBIC_DATABASE_URL not set — skipping migrator-role tests")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    yield conn
    conn.close()
