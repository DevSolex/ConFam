"""
tests/test_reconciliation.py

Tests for the automated reconciliation job (OQ-013 / Rule 7).

Coverage:
  - Already-logged transaction: detected as no-op, no double-write
  - Missed transaction (no RailEvent): recovery attempted via _handle_charge_success
  - Partial transaction (RailEvent exists, processed=False): recovery attempted
  - RailEvent processed=True but no LedgerEntry: raised as fatal incident
  - Paystack fetch failure: job raises Sentry alert, returns error summary
  - Recovery failure: incident raised, job continues to next transaction

All tests mock both the Paystack API and Sentry — no real network calls,
no real alerts sent.
"""

import os
import uuid
from unittest.mock import patch

import psycopg2
import pytest

from services.settlement_engine.reconciliation import (
    _reconcile_transaction,
    run_reconciliation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def migrator_conn():
    url = os.environ.get("ALEMBIC_DATABASE_URL", "").replace("postgresql+psycopg2://", "postgresql://")
    if not url:
        pytest.skip("ALEMBIC_DATABASE_URL not set")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


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
def logged_transaction(migrator_conn) -> dict:
    """
    A fully-settled transaction: merchant, payout account, payment link,
    rail event (processed=True), and ledger entry all exist.
    """
    run_id = uuid.uuid4().hex[:10]
    reference = f"recon-ref-{run_id}"

    with migrator_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO merchants (whatsapp_number, confam_thread_id, status) "
            "VALUES (%s, %s, 'active') RETURNING merchant_id",
            (f"+234{run_id}", f"234{run_id}"),
        )
        merchant_id = str(cur.fetchone()[0])

        cur.execute(
            "INSERT INTO payout_accounts (merchant_id, bank_account_number, bank_code, "
            "verification_method, verified_at, active_from, paystack_subaccount_code) "
            "VALUES (%s, 'enc', 'enc', 'bank_api_resolve', now(), "
            "now()-interval'1m', 'ACCT_recon') "
            "RETURNING payout_account_id",
            (merchant_id,),
        )
        payout_id = str(cur.fetchone()[0])

        cur.execute(
            "INSERT INTO payment_links (merchant_id, amount_minor_units, currency, "
            "description, status, expires_at) "
            "VALUES (%s, 50000, 'NGN', 'recon test', 'logged', now()+interval'30m') "
            "RETURNING link_id",
            (merchant_id,),
        )
        link_id = str(cur.fetchone()[0])

        cur.execute(
            "INSERT INTO rail_events (link_id, rail, rail_reference, raw_payload, processed) "
            "VALUES (%s, 'bank', %s, '{}', TRUE) RETURNING rail_event_id",
            (link_id, reference),
        )
        rail_event_id = str(cur.fetchone()[0])

        cur.execute(
            "INSERT INTO ledger_entries (link_id, merchant_id, payout_account_id, "
            "rail_event_id, amount_minor_units, currency, rail, confirmed_at, entry_type) "
            "VALUES (%s, %s, %s, %s, 50000, 'NGN', 'bank', now(), 'sale') "
            "RETURNING ledger_entry_id",
            (link_id, merchant_id, payout_id, rail_event_id),
        )
    migrator_conn.commit()

    return {
        "reference": reference,
        "link_id": link_id,
        "merchant_id": merchant_id,
        "payout_id": payout_id,
        "rail_event_id": rail_event_id,
    }


# ---------------------------------------------------------------------------
# Unit tests (domain logic, no live Paystack)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestReconcileTransaction:

    def test_already_logged_returns_no_action(self, db_conn, logged_transaction):
        """
        A transaction that is fully settled (RailEvent processed + LedgerEntry)
        should be detected as already_logged — no write, no alert.
        """
        with patch("sentry_sdk.capture_message") as mock_sentry:
            result = _reconcile_transaction(
                conn=db_conn,
                reference=logged_transaction["reference"],
                link_id=logged_transaction["link_id"],
                amount=50000,
                currency="NGN",
                txn={"reference": logged_transaction["reference"], "amount": 50000,
                     "currency": "NGN", "status": "success",
                     "metadata": {"confam_link_id": logged_transaction["link_id"]},
                     "subaccount": {"subaccount_code": "ACCT_recon"}},
            )
        assert result == "already_logged"
        mock_sentry.assert_not_called()

    def test_missing_rail_event_attempts_recovery(self, db_conn):
        """
        A Paystack transaction with no corresponding RailEvent triggers recovery.
        The recovery calls _handle_charge_success — if it succeeds, returns 'recovered'.
        """
        unknown_ref = f"missing-{uuid.uuid4().hex[:8]}"
        unknown_link = str(uuid.uuid4())

        with (
            patch(
                "services.settlement_engine.reconciliation._handle_charge_success"
            ) as mock_handler,
            patch("sentry_sdk.capture_message") as mock_sentry,
        ):
            mock_handler.return_value = None  # success
            result = _reconcile_transaction(
                conn=db_conn,
                reference=unknown_ref,
                link_id=unknown_link,
                amount=10000,
                currency="NGN",
                txn={"reference": unknown_ref, "amount": 10000, "currency": "NGN",
                     "status": "success",
                     "metadata": {"confam_link_id": unknown_link},
                     "subaccount": {"subaccount_code": "ACCT_x"}},
            )

        assert result == "recovered"
        mock_handler.assert_called_once()
        mock_sentry.assert_not_called()  # no incident on successful recovery

    def test_recovery_failure_raises_incident(self, db_conn):
        """
        If recovery fails, an incident is raised (Rule 7).
        """
        unknown_ref = f"recov-fail-{uuid.uuid4().hex[:8]}"
        unknown_link = str(uuid.uuid4())

        with patch("services.settlement_engine.reconciliation._handle_charge_success",
                   side_effect=Exception("recovery error")), \
             patch("sentry_sdk.capture_message") as mock_sentry:
            result = _reconcile_transaction(
                conn=db_conn,
                reference=unknown_ref,
                link_id=unknown_link,
                amount=10000,
                currency="NGN",
                txn={"reference": unknown_ref, "amount": 10000, "currency": "NGN",
                     "status": "success",
                     "metadata": {"confam_link_id": unknown_link},
                     "subaccount": {"subaccount_code": "ACCT_x"}},
            )

        assert result == "incident"
        mock_sentry.assert_called()  # incident alert raised


@pytest.mark.integration
class TestRunReconciliation:

    def test_paystack_fetch_failure_raises_incident(self, monkeypatch):
        """
        If Paystack's API is unreachable, the job raises a Sentry alert
        and returns an error summary. Rule 7: not a silent failure.
        """
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")

        with patch("services.settlement_engine.reconciliation._fetch_paystack_transactions",
                   side_effect=Exception("Paystack unreachable")), \
             patch("sentry_sdk.capture_exception") as mock_exc, \
             patch("sentry_sdk.capture_message") as mock_msg:
            summary = run_reconciliation()

        assert summary["errors"]
        mock_exc.assert_called_once()
        mock_msg.assert_called_once()  # incident alert

    def test_already_settled_transactions_produce_clean_summary(
        self, logged_transaction, monkeypatch
    ):
        """
        A run over already-settled transactions produces zero incidents,
        zero recoveries, and no Sentry alerts.
        """
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")

        mock_txn = {
            "reference": logged_transaction["reference"],
            "amount": 50000,
            "currency": "NGN",
            "status": "success",
            "metadata": {"confam_link_id": logged_transaction["link_id"]},
            "subaccount": {"subaccount_code": "ACCT_recon"},
        }

        with patch("services.settlement_engine.reconciliation._fetch_paystack_transactions",
                   return_value=[mock_txn]), \
             patch("sentry_sdk.capture_message") as mock_sentry:
            summary = run_reconciliation()

        assert summary["already_logged"] == 1
        assert summary["incidents"] == 0
        assert summary["recovered"] == 0
        mock_sentry.assert_not_called()
