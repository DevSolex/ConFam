"""Alembic revision 0007: re-apply the idempotent role/grant migration.

Why this revision exists:
    Databases that were already stamped at 0001 never re-run
    db/migrations/007_create_roles_and_grants.sql, so they never received:

      - The confam_app column-level UPDATE grants. 007 granted
        UPDATE (superseded_by, change_requested_at), but change_requested_at is
        created by migration 010 in a later revision, so the GRANT failed with
        "column does not exist" and aborted revision 0001 on any database
        migrated by a superuser.
      - Any grants at all on the standard deployment, where 007's superuser
        guard was false (alembic runs as confam_migrator, which owns the
        tables but is neither superuser nor createrole).

    007 is now idempotent — role creation is guarded, grants are gated on the
    confam_app role existing, and column grants are gated on the column
    existing — so re-executing it converges every database to the same state
    as a fresh install. It is a no-op on Render (no confam_app role).
"""

from pathlib import Path

from alembic import op

revision = "0007_converge_role_grants"
down_revision = "0006_render_single_role"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent
    / "db"
    / "migrations"
    / "007_create_roles_and_grants.sql"
)


def upgrade() -> None:
    op.execute(_SQL.read_text())


def downgrade() -> None:
    pass  # Grants are not revoked — revoking them would lock out the running app
