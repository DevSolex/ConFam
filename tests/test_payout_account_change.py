"""
tests/test_payout_account_change.py

Tests for the payout account change flow (OQ-021 / Rule 5 cooling-off).

Coverage:
  - Happy path: request a change, old account still active during cooling-off
  - Time-passing: new account becomes active once active_from passes
  - Reject second pending change while one is outstanding
  - Cancellation removes the pending row; old account stays active
  - Notification sent immediately on request, masked account number
  - Notification failure does NOT roll back the change request

All DB tests run as confam_app (TEST_DATABASE_URL) with migrator cleanup.
"""

import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from httpx import ASGITransport, AsyncClient

from confam.payout_accounts import (
    NoActivePayoutAccount,
    NoPendingChange,
    PendingChangeAlreadyExists,
    cancel_pending_change,
    get_active_payout_account,
    get_pending_change,
    request_payout_account_change,
)
from confam.paystack import CreatedSubaccount, ResolvedAccount
from services.settlement_engine.main import app as engine_app

# ---------------------------------------------------------------------------
# Fixtures
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
    url = os.environ.get("ALEMBIC_DATABASE_URL", "").replace("postgresql+psycopg2://", "postgresql://")
    if not url:
        pytest.skip("ALEMBIC_DATABASE_URL not set")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture()
def merchant_with_active_account(migrator_conn, db_conn) -> Iterator[dict]:
    """
    Insert a merchant with an already-active payout account.
    This is the starting state for all change-flow tests.
    """
    run_id = uuid.uuid4().hex[:10]
    thread_id = f"234800{run_id}"
    with migrator_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO merchants (whatsapp_number, confam_thread_id, business_name, status)
               VALUES (%s, %s, 'Test Store', 'active') RETURNING merchant_id""",
            (f"+{thread_id}", thread_id),
        )
        merchant_id = str(cur.fetchone()[0])
        cur.execute(
            """INSERT INTO payout_accounts (
                merchant_id, bank_account_number, bank_code,
                account_holder_name, verification_method, verified_at,
                active_from, paystack_subaccount_code
               ) VALUES (%s, 'old-acct', 'old-code', 'Old Name',
                         'bank_api_resolve', now(), now() - interval '1 minute',
                         'ACCT_old_subaccount')
               RETURNING payout_account_id""",
            (merchant_id,),
        )
        original_payout_id = str(cur.fetchone()[0])
    migrator_conn.commit()
    yield {
        "merchant_id": merchant_id,
        "thread_id": thread_id,
        "original_payout_id": original_payout_id,
        "original_subaccount": "ACCT_old_subaccount",
    }


# ---------------------------------------------------------------------------
# Domain-layer tests (confam.payout_accounts functions directly)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestPayoutAccountChangeDomain:

    def test_old_account_still_active_during_cooling_off(
        self, db_conn, migrator_conn, merchant_with_active_account
    ):
        """
        After requesting a change, get_active_payout_account() must still return
        the OLD account — the new row's active_from is in the future.
        """
        merchant_id = merchant_with_active_account["merchant_id"]
        # Use a long cooling-off so it stays pending throughout the test
        with patch.dict(os.environ, {"PAYOUT_ACCOUNT_COOLING_OFF_SECONDS": "3600"}):
            pending = request_payout_account_change(
                migrator_conn,
                merchant_id=merchant_id,
                bank_account_number="1234567890",
                bank_code="058",
                account_holder_name="New Name",
                paystack_subaccount_code="ACCT_new_subaccount",
            )

        assert pending.active_from > datetime.now(UTC)

        # Old account must still be active
        active = get_active_payout_account(db_conn, merchant_id)
        assert active.paystack_subaccount_code == "ACCT_old_subaccount"
        assert active.payout_account_id == merchant_with_active_account["original_payout_id"]

    def test_new_account_active_after_cooling_off_passes(
        self, db_conn, migrator_conn, merchant_with_active_account
    ):
        """
        With a very short cooling-off window (1 second), the new account becomes
        active automatically once time passes — no manual activation step.
        """
        merchant_id = merchant_with_active_account["merchant_id"]
        with patch.dict(os.environ, {"PAYOUT_ACCOUNT_COOLING_OFF_SECONDS": "1"}):
            pending = request_payout_account_change(
                migrator_conn,
                merchant_id=merchant_id,
                bank_account_number="9876543210",
                bank_code="033",
                account_holder_name="New Name",
                paystack_subaccount_code="ACCT_new_after_cooling",
            )

        # Wait for active_from to pass
        time.sleep(2)

        # New account should now be active
        active = get_active_payout_account(db_conn, merchant_id)
        assert active.paystack_subaccount_code == "ACCT_new_after_cooling"
        assert active.payout_account_id == pending.payout_account_id

    def test_rejects_second_pending_change(
        self, migrator_conn, merchant_with_active_account
    ):
        """Only one pending change at a time."""
        merchant_id = merchant_with_active_account["merchant_id"]
        with patch.dict(os.environ, {"PAYOUT_ACCOUNT_COOLING_OFF_SECONDS": "3600"}):
            # First change — succeeds
            request_payout_account_change(
                migrator_conn, merchant_id=merchant_id,
                bank_account_number="1111111111", bank_code="058",
                account_holder_name="Name", paystack_subaccount_code="ACCT_first",
            )
            # Second change — must be rejected
            with pytest.raises(PendingChangeAlreadyExists):
                request_payout_account_change(
                    migrator_conn, merchant_id=merchant_id,
                    bank_account_number="2222222222", bank_code="058",
                    account_holder_name="Name", paystack_subaccount_code="ACCT_second",
                )

    def test_cancellation_removes_pending_row_old_account_stays_active(
        self, db_conn, migrator_conn, merchant_with_active_account
    ):
        """
        After cancellation: no pending change, old account is still active indefinitely.
        """
        merchant_id = merchant_with_active_account["merchant_id"]
        with patch.dict(os.environ, {"PAYOUT_ACCOUNT_COOLING_OFF_SECONDS": "3600"}):
            request_payout_account_change(
                migrator_conn, merchant_id=merchant_id,
                bank_account_number="3333333333", bank_code="058",
                account_holder_name="Name", paystack_subaccount_code="ACCT_to_cancel",
            )

        # Pending change exists
        assert get_pending_change(db_conn, merchant_id) is not None

        cancel_pending_change(migrator_conn, merchant_id)

        # Pending change gone
        assert get_pending_change(db_conn, merchant_id) is None

        # Old account still active
        active = get_active_payout_account(db_conn, merchant_id)
        assert active.paystack_subaccount_code == "ACCT_old_subaccount"

    def test_cancel_when_nothing_pending_raises(
        self, migrator_conn, merchant_with_active_account
    ):
        with pytest.raises(NoPendingChange):
            cancel_pending_change(migrator_conn, merchant_with_active_account["merchant_id"])

    def test_change_on_merchant_with_no_active_account_raises(self, migrator_conn):
        """The change endpoint requires an existing active account."""
        # Merchant with no payout account at all
        run_id = uuid.uuid4().hex[:10]
        with migrator_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO merchants (whatsapp_number, confam_thread_id, status) "
                "VALUES (%s, %s, 'active') RETURNING merchant_id",
                (f"+234{run_id}", f"234{run_id}"),
            )
            merchant_id = str(cur.fetchone()[0])
        migrator_conn.commit()

        with pytest.raises(NoActivePayoutAccount):
            request_payout_account_change(
                migrator_conn, merchant_id=merchant_id,
                bank_account_number="1234567890", bank_code="058",
                account_holder_name="Name", paystack_subaccount_code="ACCT_x",
            )

    def test_rls_confam_app_cannot_delete_active_row(
        self, db_conn, merchant_with_active_account
    ):
        """
        RLS proof: confam_app can delete a pending row (active_from > now())
        but is structurally blocked from deleting an already-active row
        (active_from <= now()), even with a direct DELETE statement.

        This test proves the guarantee from migration 011 holds at the database
        level — no application-code invariant, no trust in the caller.
        """
        original_payout_id = merchant_with_active_account["original_payout_id"]

        # Attempt to delete the active row as confam_app — must be blocked by RLS
        with db_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM payout_accounts WHERE payout_account_id = %s",
                (original_payout_id,),
            )
            deleted = cur.rowcount
        db_conn.rollback()  # clean up regardless

        assert deleted == 0, (
            "RLS violation: confam_app deleted an active payout account row. "
            "Migration 011 RLS policy should have blocked this DELETE."
        )

        # Confirm the row still exists
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM payout_accounts WHERE payout_account_id = %s",
                (original_payout_id,),
            )
            count = cur.fetchone()[0]
        assert count == 1, (
            "Active payout account row was deleted despite RLS — migration 011 not applied?"
        )


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestPayoutAccountChangeEndpoints:

    MOCK_RESOLVED = ResolvedAccount(
        account_number="1234567890", account_name="New Holder Name", bank_id=9
    )

    @pytest.mark.asyncio
    async def test_change_request_returns_202_with_future_active_from(
        self, merchant_with_active_account, monkeypatch
    ):
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")
        monkeypatch.setenv("PAYOUT_ACCOUNT_COOLING_OFF_SECONDS", "3600")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")

        merchant_id = merchant_with_active_account["merchant_id"]
        mock_sub = CreatedSubaccount(subaccount_code="ACCT_new_change", business_name="New")

        with (
            patch(
                "services.settlement_engine.onboarding.resolve_bank_account",
                return_value=self.MOCK_RESOLVED,
            ),
            patch("services.settlement_engine.onboarding.create_subaccount", return_value=mock_sub),
            patch("services.messaging.main.httpx.post") as mock_whatsapp,
        ):
            mock_whatsapp.return_value = MagicMock(status_code=200)
            mock_whatsapp.return_value.raise_for_status = MagicMock()
            async with AsyncClient(
                transport=ASGITransport(app=engine_app),
                base_url="http://test",
            ) as client:
                resp = await client.post(
                    f"/merchants/{merchant_id}/payout-account/change",
                    json={"bank_account_number": "1234567890", "bank_code": "058"},
                )

        assert resp.status_code == 202
        body = resp.json()
        # active_from must be in the future
        from datetime import datetime
        active_from = datetime.fromisoformat(body["active_from"])
        assert active_from > datetime.now(UTC)
        assert body["cooling_off_seconds"] == 3600
        assert body["paystack_subaccount_code"] == "ACCT_new_change"

    @pytest.mark.asyncio
    async def test_notification_sent_with_masked_account_number(
        self, merchant_with_active_account, monkeypatch
    ):
        """
        Notification must be sent immediately. Account number must be masked
        (last 4 digits only) — do not leak full account number in the message.
        """
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")
        monkeypatch.setenv("PAYOUT_ACCOUNT_COOLING_OFF_SECONDS", "3600")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")

        merchant_id = merchant_with_active_account["merchant_id"]
        mock_sub = CreatedSubaccount(
            subaccount_code=f"ACCT_notif_{uuid.uuid4().hex[:6]}",
            business_name="New",
        )

        with (
            patch(
                "services.settlement_engine.onboarding.resolve_bank_account",
                return_value=self.MOCK_RESOLVED,
            ),
            patch("services.settlement_engine.onboarding.create_subaccount", return_value=mock_sub),
            patch("services.messaging.main.httpx.post") as mock_whatsapp,
        ):
            mock_whatsapp.return_value = MagicMock(status_code=200)
            mock_whatsapp.return_value.raise_for_status = MagicMock()
            async with AsyncClient(
                transport=ASGITransport(app=engine_app),
                base_url="http://test",
            ) as client:
                await client.post(
                    f"/merchants/{merchant_id}/payout-account/change",
                    json={"bank_account_number": "1234567890", "bank_code": "058"},
                )

        # Notification was sent
        mock_whatsapp.assert_called_once()
        sent_body = mock_whatsapp.call_args.kwargs["json"]["text"]["body"]
        # Must contain masked account (last 4 only)
        assert "7890" in sent_body         # last 4 digits
        assert "1234567890" not in sent_body  # full number must NOT appear
        assert "change" in sent_body.lower()

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_cancel_change(
        self, merchant_with_active_account, db_conn, monkeypatch
    ):
        """
        Even if the WhatsApp notification fails, the change request is persisted.
        The settlement engine must not silently drop the change because a send failed.
        (Support team is alerted via the error log instead.)
        """
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")
        monkeypatch.setenv("PAYOUT_ACCOUNT_COOLING_OFF_SECONDS", "3600")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")

        merchant_id = merchant_with_active_account["merchant_id"]
        mock_sub = CreatedSubaccount(
            subaccount_code=f"ACCT_failnotif_{uuid.uuid4().hex[:6]}",
            business_name="New",
        )

        with (
            patch(
                "services.settlement_engine.onboarding.resolve_bank_account",
                return_value=self.MOCK_RESOLVED,
            ),
            patch("services.settlement_engine.onboarding.create_subaccount", return_value=mock_sub),
            patch(
                "services.messaging.main.httpx.post",
                side_effect=Exception("WhatsApp unreachable"),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=engine_app),
                base_url="http://test",
            ) as client:
                resp = await client.post(
                    f"/merchants/{merchant_id}/payout-account/change",
                    json={"bank_account_number": "1234567890", "bank_code": "058"},
                )

        # Change still persisted despite notification failure
        assert resp.status_code == 202
        assert get_pending_change(db_conn, merchant_id) is not None

    @pytest.mark.asyncio
    async def test_cancel_endpoint_removes_pending_change(
        self, merchant_with_active_account, db_conn, monkeypatch
    ):
        """Cancel endpoint uses confam_app only — no elevated credential."""
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")
        monkeypatch.setenv("PAYOUT_ACCOUNT_COOLING_OFF_SECONDS", "3600")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")

        merchant_id = merchant_with_active_account["merchant_id"]
        mock_sub = CreatedSubaccount(
            subaccount_code=f"ACCT_tocancel_{uuid.uuid4().hex[:6]}",
            business_name="New",
        )

        with (
            patch(
                "services.settlement_engine.onboarding.resolve_bank_account",
                return_value=self.MOCK_RESOLVED,
            ),
            patch("services.settlement_engine.onboarding.create_subaccount", return_value=mock_sub),
            patch("services.messaging.main.httpx.post") as mock_whatsapp,
        ):
            mock_whatsapp.return_value = MagicMock(status_code=200)
            mock_whatsapp.return_value.raise_for_status = MagicMock()
            async with AsyncClient(
                transport=ASGITransport(app=engine_app),
                base_url="http://test",
            ) as client:
                await client.post(
                    f"/merchants/{merchant_id}/payout-account/change",
                    json={"bank_account_number": "1234567890", "bank_code": "058"},
                )
                # Now cancel
                cancel_resp = await client.post(
                    f"/merchants/{merchant_id}/payout-account/change/cancel",
                )

        assert cancel_resp.status_code == 200
        assert get_pending_change(db_conn, merchant_id) is None

    @pytest.mark.asyncio
    async def test_second_pending_change_rejected_409(
        self, merchant_with_active_account, monkeypatch
    ):
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")
        monkeypatch.setenv("PAYOUT_ACCOUNT_COOLING_OFF_SECONDS", "3600")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")

        merchant_id = merchant_with_active_account["merchant_id"]

        def make_mock_sub():
            return CreatedSubaccount(
                subaccount_code=f"ACCT_{uuid.uuid4().hex[:8]}",
                business_name="New",
            )

        with (
            patch(
                "services.settlement_engine.onboarding.resolve_bank_account",
                return_value=self.MOCK_RESOLVED,
            ),
            patch(
                "services.settlement_engine.onboarding.create_subaccount",
                side_effect=lambda **kw: make_mock_sub(),
            ),
            patch("services.messaging.main.httpx.post") as mock_whatsapp,
        ):
            mock_whatsapp.return_value = MagicMock(status_code=200)
            mock_whatsapp.return_value.raise_for_status = MagicMock()
            async with AsyncClient(
                transport=ASGITransport(app=engine_app),
                base_url="http://test",
            ) as client:
                resp1 = await client.post(
                    f"/merchants/{merchant_id}/payout-account/change",
                    json={"bank_account_number": "1234567890", "bank_code": "058"},
                )
                resp2 = await client.post(
                    f"/merchants/{merchant_id}/payout-account/change",
                    json={"bank_account_number": "9876543210", "bank_code": "033"},
                )

        assert resp1.status_code == 202
        assert resp2.status_code == 409
