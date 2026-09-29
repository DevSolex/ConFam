"""Alembic revision 0010: add pending_bank_selection columns to merchants.

Adds two nullable columns to merchants:
  - pending_bank_code TEXT          — the bank code a merchant tapped
  - pending_bank_selected_at TIMESTAMPTZ — when they tapped it

Both are NULL for all existing merchants. They are populated by the
interactive ONBOARD list-tap flow and cleared after onboarding completes
or the 10-minute TTL expires.
"""

from pathlib import Path

from alembic import op

revision = "0010_pending_bank_selection"
down_revision = "0009_add_merchant_country"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent
    / "db" / "migrations" / "015_pending_bank_selection.sql"
)


def upgrade() -> None:
    op.execute(_SQL.read_text())


def downgrade() -> None:
    op.execute(
        "ALTER TABLE merchants DROP COLUMN IF EXISTS pending_bank_selected_at;"
    )
    op.execute(
        "ALTER TABLE merchants DROP COLUMN IF EXISTS pending_bank_code;"
    )
