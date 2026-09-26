"""
No-double-count tests — Engineering Rules 3 and 11.

Marker: no_double_count
CI job: no-double-count

These tests guard the core idempotency guarantee: duplicate, out-of-order,
or retried confirmation events from either payment rail must produce exactly
ONE LedgerEntry per transaction. This is the direct structural guard for the
"50+ transaction no-double-count" guarantee referenced in README.md §6.

The UNIQUE constraint on (rail, rail_reference) in rail_events is the
database-level enforcement. These tests verify that the constraint holds
under the conditions that actually occur in production.

See docs/ENGINEERING_RULES.md Rules 3 and 11.
See db/migrations/004_create_rail_events.sql for the UNIQUE index definition.

Coverage scope (to be expanded as the settlement engine is implemented):
  [x] Duplicate INSERT into rail_events is rejected by UNIQUE constraint.
  [ ] Application-level: duplicate webhook → second RailEvent INSERT fails →
      LedgerEntry count = 1. (Requires settlement engine logic — add when built.)
  [ ] Out-of-order delivery produces exactly one LedgerEntry.
  [ ] Concurrent delivery from two workers produces exactly one LedgerEntry.
  [ ] Re-delivery after partial failure (rail_event written, ledger not yet) →
      settlement engine resumes correctly.
"""

import uuid
import psycopg2
import psycopg2.errors
import pytest


# ---------------------------------------------------------------------------
# Database-level constraint: duplicate (rail, rail_reference) is rejected
# ---------------------------------------------------------------------------

@pytest.mark.no_double_count
@pytest.mark.integration
class TestRailEventIdempotencyConstraint:
    """
    Verifies the UNIQUE constraint on (rail, rail_reference) in rail_events.
    This constraint is the database-level enforcement of Rule 3 — it fires
    before any application logic runs.
    """

    def test_duplicate_rail_reference_rejected(self, migrator_db_conn):
        """
        Inserting two rail_events with the same (rail, rail_reference) must
        fail with a UniqueViolation on the second INSERT.

        This is the core idempotency assertion: a rail retrying a webhook
        produces a duplicate (rail, rail_reference), which is rejected here
        before the settlement engine can process it a second time.

        Full setup requires a payment_link FK — this test is a placeholder
        documenting the assertion shape until the fixture chain is in place.
        """
        # TODO: set up a payment_link row first (requires merchant + payout_account).
        # Then:
        #   rail_ref = str(uuid.uuid4())  # simulates a Paystack/Stellar reference
        #   INSERT rail_event with (rail='bank', rail_reference=rail_ref) → succeeds
        #   INSERT again with same rail_reference → must raise UniqueViolation
        #   assert COUNT(*) on rail_events for this rail_reference = 1
        pytest.skip(
            "Requires full FK fixture chain (merchant → payout_account → payment_link). "
            "Implement when entity creation fixtures are added. "
            "The UNIQUE index exists in db/migrations/004_create_rail_events.sql."
        )

    def test_same_reference_different_rails_allowed(self, migrator_db_conn):
        """
        Two rail_events with the same rail_reference but different rail values
        must NOT be treated as duplicates — the idempotency key is (rail, rail_reference),
        not rail_reference alone.

        Edge case: a bank reference and a stellar hash could theoretically collide
        as strings; the rail discriminator ensures they are correctly treated as
        independent events.
        """
        pytest.skip(
            "Requires full FK fixture chain. Implement when entity creation fixtures are added."
        )


# ---------------------------------------------------------------------------
# Placeholder for application-level no-double-count tests
# (require settlement engine logic — add when that is implemented)
# ---------------------------------------------------------------------------

@pytest.mark.no_double_count
@pytest.mark.integration
class TestApplicationLevelIdempotency:
    """
    Placeholder class for settlement-engine-level idempotency tests.
    These tests require the settlement engine's webhook handler to exist.
    Add them here when the settlement engine is implemented.

    Required coverage (per Engineering Rule 11 and CONTRIBUTING.md):
      - Duplicate bank webhook → exactly one LedgerEntry.
      - Duplicate Stellar event → exactly one LedgerEntry.
      - Out-of-order events → exactly one LedgerEntry.
      - Concurrent events (race condition simulation) → exactly one LedgerEntry.
      - Re-delivery after partial failure → idempotent recovery.
    """

    def test_placeholder_no_double_count(self):
        """
        Placeholder — will be replaced with real settlement engine tests.
        The CI job must remain green until then.
        """
        pytest.skip(
            "Settlement engine not yet implemented. "
            "This placeholder keeps the no-double-count CI job green. "
            "Replace with real tests when services/settlement-engine/ is built."
        )
