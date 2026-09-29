"""Alembic revision 0006: Render single-role RLS adaptation."""

from pathlib import Path

from alembic import op

revision = "0006_render_single_role"
down_revision = "0005_payout_accounts_rls"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent
    / "db"
    / "migrations"
    / "012_render_single_role_adaptation.sql"
)


def upgrade() -> None:
    op.execute(_SQL.read_text())


def downgrade() -> None:
    pass  # Conditional — no downgrade needed
