"""
Alembic revision: 0002_add_subaccount_to_payout_accounts

Adds paystack_subaccount_code to the payout_accounts table.
See db/migrations/008_add_subaccount_to_payout_accounts.sql.
"""

from pathlib import Path
from alembic import op

revision = "0002_add_subaccount"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "db" / "migrations"


def upgrade() -> None:
    sql = (_MIGRATIONS_DIR / "008_add_subaccount_to_payout_accounts.sql").read_text()
    op.execute(sql)


def downgrade() -> None:
    op.execute("ALTER TABLE payout_accounts DROP COLUMN IF EXISTS paystack_subaccount_code;")
