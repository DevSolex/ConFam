"""Alembic revision 0003: add merchant onboarding fields."""

from pathlib import Path

from alembic import op

revision = "0003_onboarding_fields"
down_revision = "0002_add_subaccount"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent
    / "db"
    / "migrations"
    / "009_add_merchant_onboarding_fields.sql"
)


def upgrade() -> None:
    op.execute(_SQL.read_text())


def downgrade() -> None:
    op.execute("ALTER TABLE payout_accounts DROP COLUMN IF EXISTS account_holder_name;")
    op.execute("ALTER TABLE merchants DROP COLUMN IF EXISTS business_name;")
