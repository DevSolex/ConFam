"""Initial schema: migrations 001–007

Revision ID: 0001_initial_schema
Revises: (none)
Create Date: 2026-09-17

Applies the seven plain-SQL migration files from db/migrations/ in order.
Each file is executed via op.execute() — no ORM autogenerate.
See alembic/env.py for rationale on the raw-SQL approach.
"""

from pathlib import Path

from alembic import op

# Revision identifiers used by Alembic.
revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

# Path to the plain-SQL migration files.
_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "db" / "migrations"

# Ordered list of SQL files this revision applies.
# Add new files here as the schema grows — each subsequent Alembic revision
# should reference only the SQL files added since the previous revision.
_SQL_FILES = [
    "001_create_merchants.sql",
    "002_create_payout_accounts.sql",
    "003_create_payment_links.sql",
    "004_create_rail_events.sql",
    "005_create_ledger_entries.sql",
    "006_harden_ledger_append_only.sql",
    "007_create_roles_and_grants.sql",
]


def upgrade() -> None:
    for filename in _SQL_FILES:
        sql = (_MIGRATIONS_DIR / filename).read_text()
        op.execute(sql)


def downgrade() -> None:
    # Downgrade for the initial schema drops all tables.
    # WARNING: this is destructive. In production, run only after confirming
    # with the team — the ledger_entries table contains the product's core data.
    op.execute("""
        DROP TABLE IF EXISTS ledger_entries CASCADE;
        DROP TABLE IF EXISTS rail_events CASCADE;
        DROP TABLE IF EXISTS payment_links CASCADE;
        DROP TABLE IF EXISTS payout_accounts CASCADE;
        DROP TABLE IF EXISTS merchants CASCADE;
        DROP FUNCTION IF EXISTS ledger_entries_enforce_append_only CASCADE;
        DROP ROLE IF EXISTS confam_app;
        DROP ROLE IF EXISTS confam_migrator;
    """)
