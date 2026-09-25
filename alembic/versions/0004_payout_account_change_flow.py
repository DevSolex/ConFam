"""Alembic revision 0004: payout account change flow setup."""

from pathlib import Path
from alembic import op

revision = "0004_payout_account_change_flow"
down_revision = "0003_onboarding_fields"
branch_labels = None
depends_on = None

_SQL = Path(__file__).parent.parent.parent / "db" / "migrations" / "010_payout_account_change_flow.sql"


def upgrade() -> None:
    op.execute(_SQL.read_text())


def downgrade() -> None:
    op.execute("ALTER TABLE payout_accounts DROP COLUMN IF EXISTS change_requested_at;")
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS payout_accounts_one_active_per_merchant
            ON payout_accounts (merchant_id)
            WHERE superseded_by IS NULL AND active_from IS NOT NULL;
    """)
