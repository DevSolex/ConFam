"""
tests/test_onboarding.py

Tests for merchant creation and payout account verification endpoints.

Coverage:
  - POST /merchants: creates merchant, returns merchant_id + pending_verification
  - POST /merchants: rejects duplicate whatsapp_number
  - POST /merchants/{id}/payout-account: happy path — resolves account,
    creates subaccount, writes PayoutAccount, activates merchant
  - POST /merchants/{id}/payout-account: bad bank details → 422, no PayoutAccount row
  - POST /merchants/{id}/payout-account: subaccount creation fails after
    successful resolution → no PayoutAccount row (atomicity)
  - POST /merchants/{id}/payout-account: already-active merchant → 409
  - End-to-end: onboard via API, then run the full payment-link → webhook flow

All DB assertions use confam_app (TEST_DATABASE_URL) to prove OQ-018 grants
are sufficient for the onboarding write path too.
"""

import json
import os
import uuid
import hashlib
import hmac
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from httpx import AsyncClient, ASGITransport

from confam.paystack import CreatedSubaccount, ResolvedAccount, PaystackError
from services.settlement_engine.main import app as engine_app
from services.checkout.main import app as checkout_app


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


def _sign(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha512).hexdigest()


MOCK_RESOLVED = ResolvedAccount(
    account_number="0123456789",
    account_name="Test Merchant Holdings",
    bank_id=9,
)
MOCK_SUBACCOUNT = CreatedSubaccount(
    subaccount_code=f"ACCT_onboard_{uuid.uuid4().hex[:8]}",
    business_name="Test Merchant Holdings",
)


# ---------------------------------------------------------------------------
# POST /merchants
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestCreateMerchant:

    @pytest.mark.asyncio
    async def test_creates_merchant_pending_verification(self, db_conn, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        phone = f"+234-{uuid.uuid4().hex[:8]}"
        async with AsyncClient(transport=ASGITransport(app=engine_app), base_url="http://test") as client:
            resp = await client.post("/merchants", json={
                "whatsapp_number": phone,
                "business_name": "Adaeze Fashion Store",
            })
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "pending_verification"
        uuid.UUID(body["merchant_id"])  # valid UUID

        # Verify DB state as confam_app
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, business_name FROM merchants WHERE merchant_id = %s",
                (body["merchant_id"],),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "pending_verification"
        assert row[1] == "Adaeze Fashion Store"

    @pytest.mark.asyncio
    async def test_rejects_duplicate_whatsapp_number(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        phone = f"+234-dup-{uuid.uuid4().hex[:6]}"
        async with AsyncClient(transport=ASGITransport(app=engine_app), base_url="http://test") as client:
            resp1 = await client.post("/merchants", json={
                "whatsapp_number": phone, "business_name": "First Store",
            })
            assert resp1.status_code == 201
            resp2 = await client.post("/merchants", json={
                "whatsapp_number": phone, "business_name": "Second Store",
            })
        assert resp2.status_code == 409


# ---------------------------------------------------------------------------
# POST /merchants/{id}/payout-account
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestSubmitPayoutAccount:

    @pytest.mark.asyncio
    async def test_happy_path_resolves_creates_activates(self, db_conn, monkeypatch):
        """
        Full onboarding: merchant created, payout account submitted,
        Paystack mocked to succeed — merchant becomes active.
        """
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")

        # Create merchant
        phone = f"+234-ob-{uuid.uuid4().hex[:8]}"
        async with AsyncClient(transport=ASGITransport(app=engine_app), base_url="http://test") as client:
            resp = await client.post("/merchants", json={
                "whatsapp_number": phone, "business_name": "Chidi Electronics",
            })
        merchant_id = resp.json()["merchant_id"]

        # Submit payout account (mock both Paystack calls)
        mock_subaccount = CreatedSubaccount(
            subaccount_code=f"ACCT_test_{uuid.uuid4().hex[:8]}",
            business_name="Chidi Electronics",
        )
        with patch("services.settlement_engine.onboarding.resolve_bank_account", return_value=MOCK_RESOLVED), \
             patch("services.settlement_engine.onboarding.create_subaccount", return_value=mock_subaccount):
            async with AsyncClient(transport=ASGITransport(app=engine_app), base_url="http://test") as client:
                resp = await client.post(f"/merchants/{merchant_id}/payout-account", json={
                    "bank_account_number": "0123456789",
                    "bank_code": "058",
                })

        assert resp.status_code == 201
        body = resp.json()
        assert body["merchant_status"] == "active"
        assert body["paystack_subaccount_code"] == mock_subaccount.subaccount_code
        assert body["account_holder_name"] == MOCK_RESOLVED.account_name

        # Verify DB state as confam_app
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM merchants WHERE merchant_id = %s", (merchant_id,))
            assert cur.fetchone()[0] == "active"

            cur.execute(
                """SELECT paystack_subaccount_code, account_holder_name,
                          verification_method, active_from
                   FROM payout_accounts WHERE merchant_id = %s""",
                (merchant_id,),
            )
            pa = cur.fetchone()
        assert pa is not None
        assert pa[0] == mock_subaccount.subaccount_code
        assert pa[1] == MOCK_RESOLVED.account_name
        assert pa[2] == "bank_api_resolve"
        assert pa[3] is not None  # active_from set immediately (first-time, Rule 5)

    @pytest.mark.asyncio
    async def test_bad_bank_details_rejected_no_payout_row(self, db_conn, monkeypatch):
        """
        If Paystack bank/resolve rejects the account, no PayoutAccount row
        is created and the merchant stays pending_verification.
        """
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")

        phone = f"+234-badrsl-{uuid.uuid4().hex[:8]}"
        async with AsyncClient(transport=ASGITransport(app=engine_app), base_url="http://test") as client:
            resp = await client.post("/merchants", json={
                "whatsapp_number": phone, "business_name": "Bad Account Store",
            })
        merchant_id = resp.json()["merchant_id"]

        with patch(
            "services.settlement_engine.onboarding.resolve_bank_account",
            side_effect=PaystackError("422: Account number not found"),
        ):
            async with AsyncClient(transport=ASGITransport(app=engine_app), base_url="http://test") as client:
                resp = await client.post(f"/merchants/{merchant_id}/payout-account", json={
                    "bank_account_number": "0000000000",
                    "bank_code": "999",
                })

        assert resp.status_code == 422

        # Merchant must still be pending_verification, no PayoutAccount written
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM merchants WHERE merchant_id = %s", (merchant_id,))
            assert cur.fetchone()[0] == "pending_verification"
            cur.execute("SELECT COUNT(*) FROM payout_accounts WHERE merchant_id = %s", (merchant_id,))
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_subaccount_failure_after_resolution_leaves_no_partial_state(self, db_conn, monkeypatch):
        """
        If subaccount creation fails after bank resolution, no PayoutAccount
        row is written and merchant stays pending_verification.
        """
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")

        phone = f"+234-subfail-{uuid.uuid4().hex[:8]}"
        async with AsyncClient(transport=ASGITransport(app=engine_app), base_url="http://test") as client:
            resp = await client.post("/merchants", json={
                "whatsapp_number": phone, "business_name": "Subaccount Fail Store",
            })
        merchant_id = resp.json()["merchant_id"]

        with patch("services.settlement_engine.onboarding.resolve_bank_account", return_value=MOCK_RESOLVED), \
             patch(
                 "services.settlement_engine.onboarding.create_subaccount",
                 side_effect=PaystackError("Paystack API error"),
             ):
            async with AsyncClient(transport=ASGITransport(app=engine_app), base_url="http://test") as client:
                resp = await client.post(f"/merchants/{merchant_id}/payout-account", json={
                    "bank_account_number": "0123456789",
                    "bank_code": "058",
                })

        assert resp.status_code == 502

        # No partial state: merchant still pending, no payout account
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM merchants WHERE merchant_id = %s", (merchant_id,))
            assert cur.fetchone()[0] == "pending_verification"
            cur.execute("SELECT COUNT(*) FROM payout_accounts WHERE merchant_id = %s", (merchant_id,))
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_duplicate_payout_submission_rejected(self, monkeypatch):
        """
        A merchant who already has an active payout account must not be able
        to submit another through this endpoint. The account-change flow
        (OQ-021) is the correct path for that.
        """
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_key")

        phone = f"+234-dup2-{uuid.uuid4().hex[:8]}"
        async with AsyncClient(transport=ASGITransport(app=engine_app), base_url="http://test") as client:
            # Create merchant
            resp = await client.post("/merchants", json={
                "whatsapp_number": phone, "business_name": "Dup Account Store",
            })
            merchant_id = resp.json()["merchant_id"]

            mock_subaccount = CreatedSubaccount(
                subaccount_code=f"ACCT_dup_{uuid.uuid4().hex[:8]}",
                business_name="Dup Account Store",
            )

            # First submission — succeeds
            with patch("services.settlement_engine.onboarding.resolve_bank_account", return_value=MOCK_RESOLVED), \
                 patch("services.settlement_engine.onboarding.create_subaccount", return_value=mock_subaccount):
                resp1 = await client.post(f"/merchants/{merchant_id}/payout-account", json={
                    "bank_account_number": "0123456789", "bank_code": "058",
                })
            assert resp1.status_code == 201

            # Second submission — must be rejected
            with patch("services.settlement_engine.onboarding.resolve_bank_account", return_value=MOCK_RESOLVED), \
                 patch("services.settlement_engine.onboarding.create_subaccount", return_value=mock_subaccount):
                resp2 = await client.post(f"/merchants/{merchant_id}/payout-account", json={
                    "bank_account_number": "0123456789", "bank_code": "058",
                })
        assert resp2.status_code == 409
        assert "OQ-021" in resp2.json()["detail"]


# ---------------------------------------------------------------------------
# End-to-end: onboard via API → create link → webhook → ledger
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestOnboardingEndToEnd:

    WEBHOOK_SECRET = "e2e_onboard_secret"

    @pytest.mark.asyncio
    async def test_full_flow_from_onboarding_to_ledger(self, db_conn, monkeypatch):
        """
        End-to-end: onboard a merchant through real endpoints, then run the
        complete payment-link → checkout-open → Paystack webhook flow.
        Confirms the previous task's payment logic works against a merchant
        onboarded via API rather than via direct SQL insert.
        """
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.WEBHOOK_SECRET)
        monkeypatch.setenv("CHECKOUT_BASE_URL", "http://localhost:8001")

        mock_subaccount_code = f"ACCT_e2e_{uuid.uuid4().hex[:8]}"
        mock_subaccount = CreatedSubaccount(
            subaccount_code=mock_subaccount_code,
            business_name="E2E Test Store",
        )

        async with AsyncClient(transport=ASGITransport(app=engine_app), base_url="http://test") as engine, \
                   AsyncClient(transport=ASGITransport(app=checkout_app), base_url="http://test") as checkout:

            # Step 1: create merchant
            phone = f"+234-e2e-{uuid.uuid4().hex[:8]}"
            resp = await engine.post("/merchants", json={
                "whatsapp_number": phone, "business_name": "E2E Test Store",
            })
            assert resp.status_code == 201
            merchant_id = resp.json()["merchant_id"]

            # Step 2: onboard payout account (mocked Paystack)
            with patch("services.settlement_engine.onboarding.resolve_bank_account", return_value=MOCK_RESOLVED), \
                 patch("services.settlement_engine.onboarding.create_subaccount", return_value=mock_subaccount):
                resp = await engine.post(f"/merchants/{merchant_id}/payout-account", json={
                    "bank_account_number": "0123456789", "bank_code": "058",
                })
            assert resp.status_code == 201
            assert resp.json()["merchant_status"] == "active"

            # Step 3: create payment link
            resp = await engine.post("/links", json={
                "merchant_id": merchant_id,
                "amount_minor_units": 60000,
                "currency": "NGN",
                "description": "E2E test item",
            })
            assert resp.status_code == 201
            link_id = resp.json()["link_id"]

            # Step 4: open checkout (created → opened)
            resp = await checkout.get(f"/{link_id}/json")
            assert resp.status_code == 200
            assert resp.json()["status"] == "opened"

            # Step 5: simulate Paystack charge.success webhook
            reference = f"e2e-ref-{uuid.uuid4()}"
            payload = {
                "event": "charge.success",
                "data": {
                    "reference": reference,
                    "amount": 60000,
                    "currency": "NGN",
                    "status": "success",
                    "metadata": {"confam_link_id": link_id},
                    "subaccount": {"subaccount_code": mock_subaccount_code},
                },
            }
            payload_bytes = json.dumps(payload).encode()
            sig = hmac.new(self.WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha512).hexdigest()
            resp = await engine.post(
                "/webhooks/paystack",
                content=payload_bytes,
                headers={"Content-Type": "application/json", "x-paystack-signature": sig},
            )
            assert resp.status_code == 200

        # Step 6: verify final state in DB (confam_app role)
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM payment_links WHERE link_id = %s", (link_id,))
            assert cur.fetchone()[0] == "logged"

            cur.execute(
                "SELECT amount_minor_units, rail, entry_type FROM ledger_entries WHERE link_id = %s",
                (link_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 60000
            assert row[1] == "bank"
            assert row[2] == "sale"

            # One RailEvent, one LedgerEntry (Rule 3/11)
            cur.execute("SELECT COUNT(*) FROM rail_events WHERE rail_reference = %s", (reference,))
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE link_id = %s AND entry_type='sale'",
                (link_id,),
            )
            assert cur.fetchone()[0] == 1
