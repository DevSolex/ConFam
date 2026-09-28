"""Alembic revision 0008: flag what we did with each rail event.

Why this revision exists:
    The buyer-facing checkout now lets one link be paid more than once (card,
    then bank transfer). A second charge.success for an already-logged link is
    a surplus payment that must be refunded by hand, and until now the handler
    returned early and recorded nothing at all. rail_events.processed cannot
    express that state: FALSE is the reconciliation job's "retry me" signal and
    TRUE is indistinguishable from a normally applied sale.

    db/migrations/013_flag_rail_event_disposition.sql adds one column,
    rail_events.disposition. It is written at INSERT time only, so confam_app
    needs no new privilege — deliberately, because column-level UPDATE grants
    are what crashed migration 0007 on Render twice (see commits a9be350 and
    dea9af3). This revision is a straight re-execution of the idempotent SQL.
"""

from pathlib import Path
from alembic import op

revision = "0008_rail_event_disposition"
down_revision = "0007_converge_role_grants"
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).parent.parent.parent
    / "db"
    / "migrations"
    / "013_flag_rail_event_disposition.sql"
)


def upgrade() -> None:
    op.execute(_SQL.read_text())


def downgrade() -> None:
    # Deliberately not implemented. Dropping disposition would silently
    # destroy the record of which payments need refunding, and would
    # reintroduce the "duplicate payment is invisible" bug this column
    # exists to close. Reverting code that writes it is the operator's call.
    pass
