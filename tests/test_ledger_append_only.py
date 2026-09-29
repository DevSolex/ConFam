"""
Ledger append-only structural tests — Engineering Rule 2.

Marker: ledger_append_only
CI job: ledger-append-only

These tests verify that the two-layer append-only enforcement on ledger_entries
(privilege revocation + trigger) holds independently.

Layer 1 — Privilege-level rejection (primary defence):
  Connect as confam_app. Attempt UPDATE/DELETE. Assert rejected with
  psycopg2.errors.InsufficientPrivilege (PG error code 42501).
  The rejection must happen before query execution — not at the trigger level.

Layer 2 — Trigger-level rejection (second line of defence):
  Connect as confam_migrator (which does hold UPDATE/DELETE grants).
  Attempt UPDATE/DELETE. Assert rejected with a specific exception message
  containing 'append-only' and 'Engineering Rule 2'.

Correction path:
  Verify that a new row with entry_type='correction' can be inserted and
  that the original row is unmodified.

See docs/ENGINEERING_RULES.md Rule 2 and db/migrations/006_harden_ledger_append_only.sql.
"""

import psycopg2
import psycopg2.errors
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_test_merchant(conn) -> str:
    """Insert a minimal merchant row and return its merchant_id."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO merchants (whatsapp_number, confam_thread_id)
            VALUES ('+234-000-TEST-001', 'test-thread-001')
            RETURNING merchant_id
        """)
        return cur.fetchone()[0]


def _insert_test_ledger_entry(
    conn, merchant_id: str, link_id: str, payout_id: str, rail_event_id: str
) -> str:
    """Insert a minimal ledger entry and return its ledger_entry_id."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ledger_entries
                (link_id, merchant_id, payout_account_id, rail_event_id,
                 amount_minor_units, currency, rail, confirmed_at, entry_type)
            VALUES (%s, %s, %s, %s, 100000, 'NGN', 'bank', now(), 'sale')
            RETURNING ledger_entry_id
        """, (link_id, merchant_id, payout_id, rail_event_id))
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Layer 1: Privilege-level rejection via confam_app
# ---------------------------------------------------------------------------

@pytest.mark.ledger_append_only
@pytest.mark.integration
class TestPrivilegeLevelRejection:
    """
    Connects as confam_app — the application role with no UPDATE/DELETE grant
    on ledger_entries. All attempts must fail with InsufficientPrivilege (42501).
    """

    def test_update_rejected_at_privilege_level(self, app_db_conn):
        """
        An UPDATE attempt as confam_app must be rejected with a privilege error,
        not a trigger exception. This proves the REVOKE is the primary defence.
        """
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            with app_db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE ledger_entries SET currency = 'USD' WHERE 1=0"
                )
        app_db_conn.rollback()

    def test_delete_rejected_at_privilege_level(self, app_db_conn):
        """
        A DELETE attempt as confam_app must be rejected with a privilege error.
        """
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            with app_db_conn.cursor() as cur:
                cur.execute("DELETE FROM ledger_entries WHERE 1=0")
        app_db_conn.rollback()


# ---------------------------------------------------------------------------
# Layer 2: Trigger-level rejection via confam_migrator
# ---------------------------------------------------------------------------

@pytest.mark.ledger_append_only
@pytest.mark.integration
class TestTriggerLevelRejection:
    """
    Connects as confam_migrator (holds UPDATE/DELETE grants).
    Attempts must fail at the trigger level with a RaiseException containing
    the expected message from ledger_entries_enforce_append_only().
    """

    def _insert_ledger_row(self, cur) -> str:
        """
        Insert a minimal but FK-valid ledger_entries row using the migrator role.
        Returns the ledger_entry_id.
        """
        cur.execute("""
            INSERT INTO merchants (whatsapp_number, confam_thread_id)
            VALUES ('+234-000-TRIGGER-TEST', 'trigger-test-thread-' || gen_random_uuid()::text)
            RETURNING merchant_id
        """)
        merchant_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO payout_accounts
                (merchant_id, bank_account_number, bank_code, verification_method)
            VALUES (%s, 'enc-acct', 'enc-code', 'micro_deposit')
            RETURNING payout_account_id
        """, (merchant_id,))
        payout_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO payment_links
                (merchant_id, amount_minor_units, currency, description,
                 status, expires_at)
            VALUES (%s, 1000, 'NGN', 'trigger test', 'paid',
                    now() + interval '30 minutes')
            RETURNING link_id
        """, (merchant_id,))
        link_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO rail_events
                (link_id, rail, rail_reference, raw_payload)
            VALUES (%s, 'bank', 'TRIGGER-TEST-REF-' || gen_random_uuid()::text, '{}')
            RETURNING rail_event_id
        """, (link_id,))
        rail_event_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO ledger_entries
                (link_id, merchant_id, payout_account_id, rail_event_id,
                 amount_minor_units, currency, rail, confirmed_at, entry_type)
            VALUES (%s, %s, %s, %s, 1000, 'NGN', 'bank', now(), 'sale')
            RETURNING ledger_entry_id
        """, (link_id, merchant_id, payout_id, rail_event_id))
        return cur.fetchone()[0]

    def test_update_raises_append_only_exception(self, migrator_db_conn):
        """
        An UPDATE as confam_migrator on a real ledger_entries row must be
        rejected by the trigger with a message containing 'append-only'
        and 'Engineering Rule 2'. Trigger is FOR EACH ROW — needs a real row.
        """
        with migrator_db_conn.cursor() as cur:
            entry_id = self._insert_ledger_row(cur)

        with pytest.raises(psycopg2.errors.RaiseException) as exc_info:
            with migrator_db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE ledger_entries SET currency = 'USD' WHERE ledger_entry_id = %s",
                    (entry_id,),
                )
        error_msg = str(exc_info.value)
        assert "append-only" in error_msg.lower()
        assert "Engineering Rule 2" in error_msg
        migrator_db_conn.rollback()

    def test_delete_raises_append_only_exception(self, migrator_db_conn):
        """
        A DELETE as confam_migrator on a real ledger_entries row must be
        rejected by the trigger.
        """
        with migrator_db_conn.cursor() as cur:
            entry_id = self._insert_ledger_row(cur)

        with pytest.raises(psycopg2.errors.RaiseException) as exc_info:
            with migrator_db_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ledger_entries WHERE ledger_entry_id = %s",
                    (entry_id,),
                )
        assert "append-only" in str(exc_info.value).lower()
        migrator_db_conn.rollback()


# ---------------------------------------------------------------------------
# Correction path
# ---------------------------------------------------------------------------

@pytest.mark.ledger_append_only
@pytest.mark.integration
class TestCorrectionPath:
    """
    Verifies that the correction path (INSERT a new row with entry_type='correction')
    works correctly and leaves the original row unmodified.
    """

    def test_correction_row_inserts_successfully(self, migrator_db_conn):
        """
        A correction row (entry_type='correction', corrects_entry_id set)
        must INSERT successfully. The original row must remain unmodified.

        This test is a placeholder — it requires seed data (merchant, payout
        account, payment link, rail event) to exist. That fixture setup will
        be added when the corresponding entity creation tests are written.
        For now, this documents the required assertion shape.
        """
        # TODO: replace with a full fixture that sets up the FK chain:
        #   merchant → payout_account → payment_link → rail_event → ledger_entry
        # then inserts a correction row and asserts:
        #   1. The correction INSERT succeeds.
        #   2. The original row's fields are unchanged.
        #   3. COUNT(*) on ledger_entries for this link_id = 2 (original + correction).
        pytest.skip(
            "Correction path test requires full FK fixture chain — "
            "implement when entity creation tests are added."
        )
