"""Alembic revision 0009: add country to merchants.

Adds a 'country' column (TEXT NOT NULL DEFAULT 'nigeria') to the merchants
table. Existing rows are automatically back-filled to 'nigeria'. A CHECK
constraint limits values to the pilot's supported countries ('nigeria', 'ghana').

Kenya and Uganda are explicitly out of scope — see OPEN_QUESTIONS.md.
"""

from pathlib import Path

from alembic import op

revision = "0009_add_merchant_country"
down_revision = "0008_rail_event_disposition"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent
    / "db" / "migrations" / "014_add_merchant_country.sql"
)


def upgrade() -> None:
    op.execute(_SQL.read_text())


def downgrade() -> None:
    op.execute(
        "ALTER TABLE merchants DROP CONSTRAINT IF EXISTS merchants_country_check;"
    )
    op.execute("ALTER TABLE merchants DROP COLUMN IF EXISTS country;")
