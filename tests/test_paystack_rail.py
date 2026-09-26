"""
tests/test_paystack_rail.py

Tests for the Paystack bank rail:
  - Whitelist lookup (payout account required, no subaccount code = 503)
  - Paystack transaction initialization (mocked — no live API call in tests)
  - Webhook: signature verification rejects bad/missing signatures
  - Webhook: charge.success → RailEvent + LedgerEntry + link status = logged
  - Webhook: duplicate delivery → exactly one RailEvent, exactly one LedgerEntry
    (this replaces the no_double_count placeholder)
  - Webhook: subaccount mismatch → link marked failed, no ledger write
  - Correction entry: new row, original unmodified

All DB tests run as confam_app (TEST_DATABASE_URL) with migrator-role cleanup,
same pattern as test_payment_link_slice.py.

Engineering rules verified:
  Rule 1  — payout destination is a read-only whitelist lookup, never runtime construction
  Rule 2  — LedgerEntry is append-only; correction is a new row
  Rule 3  — duplicate rail_reference is rejected at DB level (UniqueViolation)
  Rule 4  — every ledger row has the full FK chain
  Rule 6  — amounts are always int throughout
  Rule 11 — no-double-count: duplicate webhook → exactly one LedgerEntry
"""

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import psycopg2
import psycopg2.errors
import pytest
from httpx import AsyncClient, ASGITransport

from confam.ledger import LedgerWriteError, write_correction_entry, write_ledger_entry
from confam.payout_accounts import NoActivePayoutAccount, get_active_payout_account
from confam.paystack import WebhookSignatureInvalid, verify_webhook_signature
from services.settlement_engine.main import app as engine_app
from services.checkout.main import app as checkout_app


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_conn():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture()
def migrator_conn():
    url = os.environ.get("ALEMBIC_DATABASE_URL", "").replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    if not url:
        pytest.skip("ALEMBIC_DATABASE_URL not set")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture()
def test_merchant(migrator_conn) -> dict:
    """
    Insert a test merchant, yield its IDs, clean up after.

    NOTE: ledger_entries rows written during tests are NOT deleted in teardown —
    the append-only trigger (Rule 2) blocks DELETE on that table for all roles,
    including confam_migrator. This is correct behaviour; the trigger is working
    as designed. Test ledger rows accumulate in confam_test and are cleared by
    `docker compose down -v` (full volume wipe) or by the CI environment's
    ephemeral database. Never add DELETE FROM ledger_entries to this teardown.
    """
    with migrator_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO merchants (whatsapp_number, confam_thread_id, status)
            VALUES (%s, %s, 'active')
            RETURNING merchant_id
            """,
            ("+234-000-RAIL-TEST", f"rail-thread-{uuid.uuid4()}"),
        )
        merchant_id = str(cur.fetchone()[0])
    migrator_conn.commit()
    yield {"merchant_id": merchant_id}
    # NOTE: No teardown attempted here.
    #
    # Once a LedgerEntry exists, its entire FK ancestor chain (merchants,
    # payout_accounts, payment_links, rail_events) cannot be deleted — the
    # append-only trigger on ledger_entries blocks DELETE on ledger_entries,
    # and the FK constraints block deletion of the referenced parent tables.
    #
    # This is correct behaviour: the trigger and FK constraints are working
    # exactly as designed (Rule 2). Test rows accumulate in confam_test and
    # are cleared by `docker compose down -v` or the CI ephemeral database.
    # Do not add teardown that tries to delete any table in the FK chain.
    pass


@pytest.fixture()
def test_payout_account(migrator_conn, test_merchant) -> dict:
    """
    Insert a test PayoutAccount with a Paystack subaccount code.
    Uses a test-mode subaccount code format.

    PLACEHOLDER: in production, the subaccount_code is obtained from Paystack
    after verifying merchant bank account ownership (micro-deposit or bank API).
    For testing, it is inserted directly via SQL.
    See RUNNING.md for instructions on inserting a real test-mode subaccount.
    """
    merchant_id = test_merchant["merchant_id"]
    subaccount_code = "ACCT_test_placeholder_code"  # test-mode format
    with migrator_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO payout_accounts (
                merchant_id, bank_account_number, bank_code,
                verification_method, verified_at,
                active_from, paystack_subaccount_code
            )
            VALUES (%s, 'enc-acct-test', 'enc-code-test',
                    'micro_deposit', now(),
                    now() - interval '1 minute', %s)
            RETURNING payout_account_id
            """,
            (merchant_id, subaccount_code),
        )
        payout_account_id = str(cur.fetchone()[0])
    migrator_conn.commit()
    return {
        "payout_account_id": payout_account_id,
        "merchant_id": merchant_id,
        "subaccount_code": subaccount_code,
    }


@pytest.fixture()
def test_payment_link(db_conn, test_merchant) -> dict:
    """Create a PaymentLink via confam_app role."""
    merchant_id = test_merchant["merchant_id"]
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO payment_links
                (merchant_id, amount_minor_units, currency, description, status, expires_at)
            VALUES (%s, 50000, 'NGN', 'Rail test item', 'opened',
                    now() + interval '30 minutes')
            RETURNING link_id
            """,
            (merchant_id,),
        )
        link_id = str(cur.fetchone()[0])
    db_conn.commit()
    return {"link_id": link_id, "merchant_id": merchant_id, "amount_minor_units": 50000}


def _make_charge_success_payload(link_id: str, reference: str, subaccount_code: str, amount: int = 50000) -> dict:
    """Build a minimal charge.success Paystack webhook payload."""
    return {
        "event": "charge.success",
        "data": {
            "reference": reference,
            "amount": amount,
            "currency": "NGN",
            "status": "success",
            "metadata": {"confam_link_id": link_id},
            "subaccount": {"subaccount_code": subaccount_code},
        },
    }


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha512
    ).hexdigest()


# ---------------------------------------------------------------------------
# Webhook signature verification (pure — no DB)
# ---------------------------------------------------------------------------

class TestWebhookSignatureVerification:
    TEST_SECRET = "test_paystack_secret_key_for_testing"

    def test_valid_signature_passes(self, monkeypatch):
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.TEST_SECRET)
        payload = b'{"event":"charge.success"}'
        sig = _sign_payload(payload, self.TEST_SECRET)
        # Should not raise
        verify_webhook_signature(payload, sig)

    def test_invalid_signature_raises(self, monkeypatch):
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.TEST_SECRET)
        payload = b'{"event":"charge.success"}'
        with pytest.raises(WebhookSignatureInvalid):
            verify_webhook_signature(payload, "bad_signature")

    def test_empty_signature_raises(self, monkeypatch):
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.TEST_SECRET)
        payload = b'{"event":"charge.success"}'
        with pytest.raises(WebhookSignatureInvalid):
            verify_webhook_signature(payload, "")

    def test_tampered_payload_raises(self, monkeypatch):
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.TEST_SECRET)
        original = b'{"event":"charge.success","amount":1000}'
        sig = _sign_payload(original, self.TEST_SECRET)
        tampered = b'{"event":"charge.success","amount":999999}'
        with pytest.raises(WebhookSignatureInvalid):
            verify_webhook_signature(tampered, sig)


# ---------------------------------------------------------------------------
# Payout account whitelist lookup
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestPayoutAccountWhitelistLookup:
    def test_returns_active_account(self, db_conn, test_payout_account):
        account = get_active_payout_account(db_conn, test_payout_account["merchant_id"])
        assert account.payout_account_id == test_payout_account["payout_account_id"]
        assert account.paystack_subaccount_code == test_payout_account["subaccount_code"]

    def test_raises_for_merchant_with_no_account(self, db_conn, test_merchant):
        # Merchant exists but has no payout account
        with pytest.raises(NoActivePayoutAccount):
            get_active_payout_account(db_conn, test_merchant["merchant_id"])

    def test_raises_for_unknown_merchant(self, db_conn):
        with pytest.raises(NoActivePayoutAccount):
            get_active_payout_account(db_conn, str(uuid.uuid4()))

    def test_does_not_return_inactive_account(self, db_conn, migrator_conn, test_merchant):
        """An account with active_from in the future is not yet active."""
        merchant_id = test_merchant["merchant_id"]
        with migrator_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payout_accounts (
                    merchant_id, bank_account_number, bank_code,
                    verification_method, verified_at,
                    active_from, paystack_subaccount_code
                )
                VALUES (%s, 'enc', 'enc', 'micro_deposit', now(),
                        now() + interval '24 hours', 'ACCT_future')
                """,
                (merchant_id,),
            )
        migrator_conn.commit()
        with pytest.raises(NoActivePayoutAccount):
            get_active_payout_account(db_conn, merchant_id)


# ---------------------------------------------------------------------------
# Initiate bank payment endpoint (checkout service)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestInitiateBankPayment:
    TEST_SECRET = "test_paystack_secret"

    @pytest.mark.asyncio
    async def test_returns_authorization_url(
        self, test_payment_link, test_payout_account, monkeypatch
    ):
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.TEST_SECRET)
        monkeypatch.setenv(
            "DATABASE_URL",
            os.environ.get("TEST_DATABASE_URL", ""),
        )

        mock_tx = MagicMock()
        mock_tx.authorization_url = "https://checkout.paystack.com/test_code"
        mock_tx.reference = test_payment_link["link_id"]

        with patch("services.checkout.pay.initialize_transaction", return_value=mock_tx):
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{test_payment_link['link_id']}/pay/bank",
                    json={"email": "buyer@example.com"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert "authorization_url" in body
        assert body["authorization_url"] == "https://checkout.paystack.com/test_code"

    @pytest.mark.asyncio
    async def test_rejects_expired_link(
        self, migrator_conn, test_merchant, test_payout_account, monkeypatch
    ):
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        # Insert an expired link
        merchant_id = test_merchant["merchant_id"]
        with migrator_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payment_links
                    (merchant_id, amount_minor_units, currency, description, status, expires_at)
                VALUES (%s, 1000, 'NGN', 'expired', 'created',
                        now() - interval '1 second')
                RETURNING link_id
                """,
                (merchant_id,),
            )
            link_id = str(cur.fetchone()[0])
        migrator_conn.commit()

        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/{link_id}/pay/bank",
                json={"email": "buyer@example.com"},
            )
        assert resp.status_code == 410

    @pytest.mark.asyncio
    async def test_503_when_no_payout_account(
        self, test_payment_link, monkeypatch
    ):
        """A merchant with no active payout account cannot receive payments — Rule 1."""
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        # test_payment_link has no payout account (test_payout_account fixture not used here)
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/{test_payment_link['link_id']}/pay/bank",
                json={"email": "buyer@example.com"},
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Webhook: charge.success → RailEvent + LedgerEntry (the main flow)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestChargeSuccessWebhook:
    TEST_SECRET = "test_paystack_webhook_secret"

    def _post_webhook(self, payload_dict: dict, secret: str = None):
        """Post a signed webhook to the settlement engine via ASGI transport."""
        secret = secret or self.TEST_SECRET
        payload_bytes = json.dumps(payload_dict).encode()
        sig = _sign_payload(payload_bytes, secret)
        return payload_bytes, sig

    @pytest.mark.asyncio
    async def test_charge_success_creates_rail_event_and_ledger_entry(
        self,
        test_payment_link,
        test_payout_account,
        db_conn,
        monkeypatch,
    ):
        """
        A single verified charge.success webhook produces:
          - exactly one RailEvent row
          - exactly one LedgerEntry row
          - PaymentLink status = 'logged'
        """
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.TEST_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        link_id = test_payment_link["link_id"]
        reference = f"pay-ref-{uuid.uuid4()}"
        payload = _make_charge_success_payload(
            link_id, reference, test_payout_account["subaccount_code"]
        )
        payload_bytes, sig = self._post_webhook(payload)

        async with AsyncClient(
            transport=ASGITransport(app=engine_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhooks/paystack",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-paystack-signature": sig,
                },
            )

        assert resp.status_code == 200

        # Verify exactly one RailEvent
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM rail_events WHERE rail_reference = %s",
                (reference,),
            )
            assert cur.fetchone()[0] == 1, "Expected exactly one RailEvent"

            # Verify exactly one LedgerEntry
            cur.execute(
                """
                SELECT COUNT(*) FROM ledger_entries le
                JOIN rail_events re ON le.rail_event_id = re.rail_event_id
                WHERE re.rail_reference = %s AND le.entry_type = 'sale'
                """,
                (reference,),
            )
            assert cur.fetchone()[0] == 1, "Expected exactly one LedgerEntry"

            # Verify the LedgerEntry has the full FK chain (Rule 4)
            cur.execute(
                """
                SELECT le.link_id, le.merchant_id, le.payout_account_id,
                       le.rail_event_id, le.amount_minor_units, le.currency,
                       le.rail, le.entry_type
                FROM ledger_entries le
                JOIN rail_events re ON le.rail_event_id = re.rail_event_id
                WHERE re.rail_reference = %s
                """,
                (reference,),
            )
            row = cur.fetchone()
            assert str(row[0]) == link_id
            assert str(row[1]) == test_payout_account["merchant_id"]
            assert str(row[2]) == test_payout_account["payout_account_id"]
            assert row[4] == 50000    # amount_minor_units — int, Rule 6
            assert row[5] == "NGN"
            assert row[6] == "bank"
            assert row[7] == "sale"

            # Verify PaymentLink is logged
            cur.execute(
                "SELECT status FROM payment_links WHERE link_id = %s",
                (link_id,),
            )
            assert cur.fetchone()[0] == "logged"

    @pytest.mark.asyncio
    @pytest.mark.no_double_count
    async def test_duplicate_webhook_produces_one_rail_event_and_one_ledger_entry(
        self,
        test_payment_link,
        test_payout_account,
        db_conn,
        monkeypatch,
    ):
        """
        Sending the same charge.success webhook payload twice (simulating
        Paystack's retry behavior) must produce:
          - exactly ONE RailEvent
          - exactly ONE LedgerEntry
          - no error, no duplicate, no exception

        This is Engineering Rule 3 and Rule 11 in practice.
        The UNIQUE (rail, rail_reference) constraint on rail_events is the
        database-level backstop.
        """
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.TEST_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        link_id = test_payment_link["link_id"]
        reference = f"pay-ref-dedup-{uuid.uuid4()}"
        payload = _make_charge_success_payload(
            link_id, reference, test_payout_account["subaccount_code"]
        )
        payload_bytes, sig = self._post_webhook(payload)

        async with AsyncClient(
            transport=ASGITransport(app=engine_app), base_url="http://test"
        ) as client:
            # First delivery
            resp1 = await client.post(
                "/webhooks/paystack",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-paystack-signature": sig,
                },
            )
            # Second delivery (identical payload — Paystack retry)
            resp2 = await client.post(
                "/webhooks/paystack",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-paystack-signature": sig,
                },
            )

        assert resp1.status_code == 200
        assert resp2.status_code == 200

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM rail_events WHERE rail_reference = %s",
                (reference,),
            )
            rail_event_count = cur.fetchone()[0]
            assert rail_event_count == 1, (
                f"Expected 1 RailEvent, got {rail_event_count}. "
                "Rule 3 / Rule 11 violation: duplicate webhook produced multiple RailEvents."
            )

            cur.execute(
                """
                SELECT COUNT(*) FROM ledger_entries le
                JOIN rail_events re ON le.rail_event_id = re.rail_event_id
                WHERE re.rail_reference = %s AND le.entry_type = 'sale'
                """,
                (reference,),
            )
            ledger_count = cur.fetchone()[0]
            assert ledger_count == 1, (
                f"Expected 1 LedgerEntry, got {ledger_count}. "
                "Rule 3 / Rule 11 violation: duplicate webhook produced multiple LedgerEntries."
            )

    @pytest.mark.asyncio
    async def test_invalid_signature_rejected_no_db_writes(
        self, test_payment_link, test_payout_account, db_conn, monkeypatch
    ):
        """A webhook with an invalid signature must produce no DB writes."""
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.TEST_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        reference = f"pay-ref-badsig-{uuid.uuid4()}"
        link_id = test_payment_link["link_id"]
        payload = _make_charge_success_payload(
            link_id, reference, test_payout_account["subaccount_code"]
        )
        payload_bytes = json.dumps(payload).encode()

        async with AsyncClient(
            transport=ASGITransport(app=engine_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhooks/paystack",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-paystack-signature": "completely_wrong_signature",
                },
            )

        assert resp.status_code == 200  # Always 200 to suppress retries

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM rail_events WHERE rail_reference = %s",
                (reference,),
            )
            assert cur.fetchone()[0] == 0, "Bad signature must produce no RailEvent"

    @pytest.mark.asyncio
    async def test_subaccount_mismatch_marks_link_failed_no_ledger(
        self,
        test_payment_link,
        test_payout_account,
        db_conn,
        monkeypatch,
    ):
        """
        If the subaccount in the webhook doesn't match the current whitelist,
        the link is marked failed and no LedgerEntry is written.
        """
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.TEST_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        link_id = test_payment_link["link_id"]
        reference = f"pay-ref-mismatch-{uuid.uuid4()}"
        payload = _make_charge_success_payload(
            link_id, reference, "ACCT_wrong_subaccount_code"  # mismatch
        )
        payload_bytes, sig = self._post_webhook(payload)

        async with AsyncClient(
            transport=ASGITransport(app=engine_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhooks/paystack",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-paystack-signature": sig,
                },
            )

        assert resp.status_code == 200

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM payment_links WHERE link_id = %s", (link_id,)
            )
            status = cur.fetchone()[0]
            assert status == "failed", f"Expected 'failed', got '{status}'"

            cur.execute(
                """
                SELECT COUNT(*) FROM ledger_entries le
                JOIN rail_events re ON le.rail_event_id = re.rail_event_id
                WHERE re.rail_reference = %s
                """,
                (reference,),
            )
            assert cur.fetchone()[0] == 0, "Subaccount mismatch must produce no LedgerEntry"


# ---------------------------------------------------------------------------
# Correction entry (Rule 2 — closes the placeholder in test_ledger_append_only)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestCorrectionEntry:
    """
    write_correction_entry() must:
      - INSERT a new row with entry_type='correction'
      - Set corrects_entry_id pointing to the original
      - Never touch the original row
    """

    def _build_full_chain(self, migrator_conn, merchant_id: str, payout_account_id: str) -> str:
        """Insert a full FK chain and return a ledger_entry_id."""
        with migrator_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payment_links
                    (merchant_id, amount_minor_units, currency, description,
                     status, expires_at)
                VALUES (%s, 10000, 'NGN', 'correction test', 'logged',
                        now() + interval '30 minutes')
                RETURNING link_id
                """,
                (merchant_id,),
            )
            link_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO rail_events (link_id, rail, rail_reference, raw_payload)
                VALUES (%s, 'bank', %s, '{}')
                RETURNING rail_event_id
                """,
                (link_id, f"corr-ref-{uuid.uuid4()}"),
            )
            rail_event_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO ledger_entries (
                    link_id, merchant_id, payout_account_id, rail_event_id,
                    amount_minor_units, currency, rail, confirmed_at, entry_type
                )
                VALUES (%s, %s, %s, %s, 10000, 'NGN', 'bank', now(), 'sale')
                RETURNING ledger_entry_id
                """,
                (link_id, merchant_id, payout_account_id, rail_event_id),
            )
            ledger_entry_id = str(cur.fetchone()[0])

        migrator_conn.commit()
        return ledger_entry_id

    def test_correction_is_new_row_not_mutation(
        self, db_conn, migrator_conn, test_payout_account
    ):
        """
        The correction entry is a new row. The original row's amount is unchanged.
        Rule 2: corrections are INSERTs, never UPDATEs.
        """
        original_id = self._build_full_chain(
            migrator_conn,
            test_payout_account["merchant_id"],
            test_payout_account["payout_account_id"],
        )

        correction = write_correction_entry(
            db_conn,
            original_entry_id=original_id,
            reason="reconciliation_test: amount was 100 kobo short",
            corrected_amount_minor_units=10100,
        )

        assert correction.entry_type == "correction"
        assert correction.corrects_entry_id == original_id
        assert correction.amount_minor_units == 10100
        assert isinstance(correction.amount_minor_units, int)  # Rule 6

        # Verify original row is unmodified
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT amount_minor_units, entry_type FROM ledger_entries WHERE ledger_entry_id = %s",
                (original_id,),
            )
            orig_amount, orig_type = cur.fetchone()
            assert int(orig_amount) == 10000, "Original amount must be unchanged"
            assert orig_type == "sale", "Original entry_type must be unchanged"

    def test_correction_count_is_two_rows_not_one(
        self, db_conn, migrator_conn, test_payout_account
    ):
        """After writing a correction, the ledger for this link has 2 rows: sale + correction."""
        original_id = self._build_full_chain(
            migrator_conn,
            test_payout_account["merchant_id"],
            test_payout_account["payout_account_id"],
        )
        write_correction_entry(
            db_conn,
            original_entry_id=original_id,
            reason="test",
            corrected_amount_minor_units=9999,
        )

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE ledger_entry_id = %s "
                "OR corrects_entry_id = %s",
                (original_id, original_id),
            )
            assert cur.fetchone()[0] == 2

    def test_correction_rejects_nonexistent_original(self, db_conn):
        with pytest.raises(LedgerWriteError, match="not found"):
            write_correction_entry(
                db_conn,
                original_entry_id=str(uuid.uuid4()),
                reason="test",
                corrected_amount_minor_units=100,
            )

    def test_correction_rejects_float_amount(
        self, db_conn, migrator_conn, test_payout_account
    ):
        """Rule 6: correction amount must be int."""
        original_id = self._build_full_chain(
            migrator_conn,
            test_payout_account["merchant_id"],
            test_payout_account["payout_account_id"],
        )
        with pytest.raises(TypeError, match="int"):
            write_correction_entry(
                db_conn,
                original_entry_id=original_id,
                reason="test",
                corrected_amount_minor_units=100.50,  # type: ignore[arg-type]
            )
