"""Alembic revision 0005: payout_accounts RLS for cancel_pending_change."""

from pathlib import Path

from alembic import op

revision = "0005_payout_accounts_rls"
down_revision = "0004_payout_account_change_flow"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent / "db" / "migrations" / "011_payout_accounts_rls_cancel.sql"
)


def upgrade() -> None:
    op.execute(_SQL.read_text())


def downgrade() -> None:
    op.execute("REVOKE DELETE ON payout_accounts FROM confam_app;")
    op.execute("DROP POLICY IF EXISTS payout_accounts_delete_pending_only ON payout_accounts;")
    op.execute("DROP POLICY IF EXISTS payout_accounts_update_policy ON payout_accounts;")
    op.execute("DROP POLICY IF EXISTS payout_accounts_insert_policy ON payout_accounts;")
    op.execute("DROP POLICY IF EXISTS payout_accounts_select_policy ON payout_accounts;")
    op.execute("ALTER TABLE payout_accounts DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER ROLE confam_migrator NOBYPASSRLS;")
