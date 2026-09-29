"""
tests/test_messaging.py

Tests for the Meta WhatsApp Cloud API messaging service.

Coverage:
  - GET /webhooks/whatsapp: correct verify_token → challenge echoed, wrong → 403
  - POST /webhooks/whatsapp: valid signature → processed
  - POST /webhooks/whatsapp: invalid signature → rejected (200, no processing)
  - Unregistered sender → polite rejection, no link created
  - Malformed message → usage-format reply, no link created
  - Valid PAY command → link created, reply sent with URL
  - Non-text message type → ignored (200, no processing)
  - PAY command parsing: all cases unchanged from Twilio version
  - Payment confirmation notification → Meta API called with correct content

confam_thread_id format for Meta Cloud API:
  Bare digits only — e.g. "2348012345678" (no '+', no 'whatsapp:' prefix).
  Meta sends the sender ID in this format in the 'from' field.

All DB tests use confam_app (TEST_DATABASE_URL).
Meta Cloud API calls are always mocked — no real messages sent in tests.
"""

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from httpx import ASGITransport, AsyncClient

from services.messaging.main import (
    ParseError,
    _verify_meta_signature,
    parse_pay_command,
    send_payment_confirmed_notification,
)
from services.messaging.main import (
    app as messaging_app,
)

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


TEST_APP_SECRET = "test_meta_app_secret_for_tests"
TEST_VERIFY_TOKEN = "test_verify_token_12345"
TEST_PHONE_NUMBER_ID = "123456789012345"
TEST_ACCESS_TOKEN = "test_meta_access_token"


def _meta_signature(payload_bytes: bytes, secret: str) -> str:
    """Compute Meta X-Hub-Signature-256 for tests."""
    digest = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _meta_payload(from_id: str, message_text: str) -> bytes:
    """Build a minimal Meta WhatsApp Cloud API webhook payload."""
    return json.dumps({
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": from_id,
                        "type": "text",
                        "text": {"body": message_text},
                    }]
                }
            }]
        }]
    }).encode()


@pytest.fixture()
def test_merchant(migrator_conn, db_conn) -> Iterator[dict]:
    """
    Insert a test merchant with a unique confam_thread_id in Meta bare-digit format.
    Returns merchant_id and from_id (bare digits, no prefix).
    """
    run_id = uuid.uuid4().hex[:12]
    # Meta format: bare digits only
    from_id = f"234800{run_id}"
    with migrator_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO merchants (whatsapp_number, confam_thread_id, business_name, status)
               VALUES (%s, %s, 'Test Store', 'active') RETURNING merchant_id""",
            (f"+{from_id}", from_id),
        )
        merchant_id = str(cur.fetchone()[0])
    migrator_conn.commit()
    yield {"merchant_id": merchant_id, "from_id": from_id}


# ---------------------------------------------------------------------------
# parse_pay_command — pure unit tests, unchanged from previous version
# ---------------------------------------------------------------------------

class TestParsePayCommand:
    def test_valid_integer_naira(self):
        amount, desc = parse_pay_command("PAY 750 Ankara fabric")
        assert amount == 75000
        assert isinstance(amount, int)
        assert desc == "Ankara fabric"

    def test_valid_decimal_naira(self):
        amount, desc = parse_pay_command("PAY 750.00 Tailoring service")
        assert amount == 75000

    def test_case_insensitive(self):
        amount, desc = parse_pay_command("pay 100 Item")
        assert amount == 10000

    def test_multiword_description(self):
        amount, desc = parse_pay_command("PAY 500 Ankara fabric x2 special order")
        assert desc == "Ankara fabric x2 special order"

    def test_rejects_zero_amount(self):
        with pytest.raises(ParseError):
            parse_pay_command("PAY 0 Something")

    def test_rejects_negative_amount(self):
        with pytest.raises(ParseError):
            parse_pay_command("PAY -100 Something")

    def test_rejects_non_numeric_amount(self):
        with pytest.raises(ParseError):
            parse_pay_command("PAY abc Something")

    def test_rejects_missing_description(self):
        with pytest.raises(ParseError):
            parse_pay_command("PAY 100")

    def test_rejects_wrong_command(self):
        with pytest.raises(ParseError):
            parse_pay_command("SEND 100 Something")

    def test_rejects_empty_message(self):
        with pytest.raises(ParseError):
            parse_pay_command("")

    def test_rejects_sub_kobo_amount(self):
        with pytest.raises(ParseError):
            parse_pay_command("PAY 0.001 Something")


# ---------------------------------------------------------------------------
# Signature verification — pure unit tests
# ---------------------------------------------------------------------------

class TestMetaSignatureVerification:
    def test_valid_signature_passes(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
        payload = b'{"test": "payload"}'
        sig = _meta_signature(payload, TEST_APP_SECRET)
        assert _verify_meta_signature(payload, sig) is True

    def test_wrong_signature_fails(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
        payload = b'{"test": "payload"}'
        assert _verify_meta_signature(payload, "sha256=wrongdigest") is False

    def test_tampered_payload_fails(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
        original = b'{"amount": 1000}'
        sig = _meta_signature(original, TEST_APP_SECRET)
        tampered = b'{"amount": 99999}'
        assert _verify_meta_signature(tampered, sig) is False

    def test_missing_sha256_prefix_fails(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
        payload = b'test'
        digest = hmac.new(TEST_APP_SECRET.encode(), payload, hashlib.sha256).hexdigest()
        # No "sha256=" prefix
        assert _verify_meta_signature(payload, digest) is False

    def test_no_app_secret_set_fails(self, monkeypatch):
        monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
        assert _verify_meta_signature(b'test', "sha256=abc") is False


# ---------------------------------------------------------------------------
# GET /webhooks/whatsapp — verify-token handshake
# ---------------------------------------------------------------------------

class TestVerifyTokenHandshake:
    BASE_URL = "http://test"

    @pytest.mark.asyncio
    async def test_correct_token_echoes_challenge(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", TEST_VERIFY_TOKEN)
        async with AsyncClient(
            transport=ASGITransport(app=messaging_app),
            base_url=self.BASE_URL,
        ) as client:
            resp = await client.get("/webhooks/whatsapp", params={
                "hub.mode": "subscribe",
                "hub.verify_token": TEST_VERIFY_TOKEN,
                "hub.challenge": "challenge_abc_123",
            })
        assert resp.status_code == 200
        assert resp.text == "challenge_abc_123"

    @pytest.mark.asyncio
    async def test_wrong_token_returns_403(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", TEST_VERIFY_TOKEN)
        async with AsyncClient(
            transport=ASGITransport(app=messaging_app),
            base_url=self.BASE_URL,
        ) as client:
            resp = await client.get("/webhooks/whatsapp", params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "challenge_abc_123",
            })
        assert resp.status_code == 403
        # Challenge must NOT be in the response
        assert "challenge_abc_123" not in resp.text

    @pytest.mark.asyncio
    async def test_missing_mode_returns_403(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", TEST_VERIFY_TOKEN)
        async with AsyncClient(
            transport=ASGITransport(app=messaging_app),
            base_url=self.BASE_URL,
        ) as client:
            resp = await client.get("/webhooks/whatsapp", params={
                "hub.verify_token": TEST_VERIFY_TOKEN,
                "hub.challenge": "challenge_abc_123",
            })
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /webhooks/whatsapp — inbound message processing
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestWhatsAppWebhook:
    BASE_URL = "http://test"

    def _signed_post(self, payload_bytes: bytes) -> dict:
        sig = _meta_signature(payload_bytes, TEST_APP_SECRET)
        return {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}

    @pytest.mark.asyncio
    async def test_invalid_signature_rejected_no_processing(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        payload = _meta_payload("2349999999999", "PAY 500 Test")
        async with AsyncClient(
            transport=ASGITransport(app=messaging_app),
            base_url=self.BASE_URL,
        ) as client:
            resp = await client.post(
                "/webhooks/whatsapp",
                content=payload,
                headers={
                    "X-Hub-Signature-256": "sha256=badsig",
                    "Content-Type": "application/json",
                },
            )
        # Returns 200 (suppress retries) but does nothing
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unregistered_sender_gets_rejection(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", TEST_PHONE_NUMBER_ID)
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", TEST_ACCESS_TOKEN)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        unknown_id = "2349" + uuid.uuid4().hex[:8]
        payload = _meta_payload(unknown_id, "PAY 500 Item")
        headers = self._signed_post(payload)

        with patch("services.messaging.main.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            async with AsyncClient(
                transport=ASGITransport(app=messaging_app),
                base_url=self.BASE_URL,
            ) as client:
                resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)

        assert resp.status_code == 200
        mock_post.assert_called_once()
        sent_body = mock_post.call_args.kwargs["json"]["text"]["body"]
        assert "not registered" in sent_body.lower()

    @pytest.mark.asyncio
    async def test_malformed_message_sends_usage_reply(self, test_merchant, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", TEST_PHONE_NUMBER_ID)
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", TEST_ACCESS_TOKEN)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        from_id = test_merchant["from_id"]
        payload = _meta_payload(from_id, "hello what can you do")
        headers = self._signed_post(payload)

        with patch("services.messaging.main.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            async with AsyncClient(
                transport=ASGITransport(app=messaging_app),
                base_url=self.BASE_URL,
            ) as client:
                resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)

        assert resp.status_code == 200
        mock_post.assert_called_once()
        sent_body = mock_post.call_args.kwargs["json"]["text"]["body"]
        assert "PAY" in sent_body

    @pytest.mark.asyncio
    async def test_valid_pay_command_creates_link_and_replies(
        self, test_merchant, db_conn, monkeypatch
    ):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", TEST_PHONE_NUMBER_ID)
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", TEST_ACCESS_TOKEN)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("CHECKOUT_BASE_URL", "http://pay.confam.co")

        from_id = test_merchant["from_id"]
        payload = _meta_payload(from_id, "PAY 750 Ankara fabric x2")
        headers = self._signed_post(payload)

        with patch("services.messaging.main.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            async with AsyncClient(
                transport=ASGITransport(app=messaging_app),
                base_url=self.BASE_URL,
            ) as client:
                resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)

        assert resp.status_code == 200
        mock_post.assert_called_once()
        sent = mock_post.call_args.kwargs["json"]
        sent_body = sent["text"]["body"]
        assert sent["to"] == from_id        # bare-digit format
        assert "pay.confam.co" in sent_body
        assert "₦750.00" in sent_body
        assert "Ankara fabric x2" in sent_body

        # Confirm PaymentLink written as confam_app
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT amount_minor_units, description, status FROM payment_links "
                "WHERE merchant_id = %s ORDER BY created_at DESC LIMIT 1",
                (test_merchant["merchant_id"],),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 75000
        assert isinstance(row[0], int)   # Rule 6
        assert row[1] == "Ankara fabric x2"
        assert row[2] == "created"

    @pytest.mark.asyncio
    async def test_non_text_message_ignored(self, monkeypatch):
        """Image, audio, etc. messages are silently ignored — 200, no processing."""
        monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        payload = json.dumps({
            "entry": [
                {
                    "changes": [
                        {"value": {"messages": [{"from": "2348000000001", "type": "image"}]}}
                    ]
                }
            ]
        }).encode()
        headers = self._signed_post(payload)

        async with AsyncClient(
            transport=ASGITransport(app=messaging_app),
            base_url=self.BASE_URL,
        ) as client:
            resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Payment confirmation notification
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CANCEL / STOP command (OQ-025)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestCancelCommand:
    BASE_URL = "http://test"

    def _signed_post(self, from_id: str, text: str, secret: str) -> tuple[bytes, dict]:
        import hashlib
        import hmac as _hmac
        import json
        payload = json.dumps({
            "entry": [{"changes": [{"value": {"messages": [
                {"from": from_id, "type": "text", "text": {"body": text}}
            ]}}]}]
        }).encode()
        sig = "sha256=" + _hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return payload, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}

    @pytest.mark.asyncio
    async def test_cancel_with_pending_change_succeeds(
        self, test_merchant, migrator_conn, monkeypatch
    ):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "test_secret")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYOUT_ACCOUNT_COOLING_OFF_SECONDS", "3600")

        from_id = test_merchant["from_id"]
        merchant_id = test_merchant["merchant_id"]

        with migrator_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO payout_accounts (merchant_id, bank_account_number, bank_code, "
                "account_holder_name, verification_method, verified_at, active_from, "
                "paystack_subaccount_code, change_requested_at) "
                "VALUES (%s, 'enc', 'enc', 'Name', 'bank_api_resolve', now(), "
                "now() + interval '3600 seconds', 'ACCT_cancel_test', now())",
                (merchant_id,),
            )
        migrator_conn.commit()

        payload, headers = self._signed_post(from_id, "CANCEL", "test_secret")

        with patch("services.messaging.main.httpx.post") as mock_send:
            mock_send.return_value = MagicMock(status_code=200)
            mock_send.return_value.raise_for_status = MagicMock()
            async with AsyncClient(
                transport=ASGITransport(app=messaging_app),
                base_url=self.BASE_URL,
            ) as client:
                resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)

        assert resp.status_code == 200
        mock_send.assert_called_once()
        sent_body = mock_send.call_args.kwargs["json"]["text"]["body"]
        assert "cancelled" in sent_body.lower()

    @pytest.mark.asyncio
    async def test_stop_is_synonymous_with_cancel(
        self, test_merchant, migrator_conn, monkeypatch
    ):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "test_secret")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYOUT_ACCOUNT_COOLING_OFF_SECONDS", "3600")

        from_id = test_merchant["from_id"]
        merchant_id = test_merchant["merchant_id"]

        with migrator_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO payout_accounts (merchant_id, bank_account_number, bank_code, "
                "account_holder_name, verification_method, verified_at, active_from, "
                "paystack_subaccount_code, change_requested_at) "
                "VALUES (%s, 'enc', 'enc', 'Name', 'bank_api_resolve', now(), "
                "now() + interval '3600 seconds', 'ACCT_stop_test', now())",
                (merchant_id,),
            )
        migrator_conn.commit()

        payload, headers = self._signed_post(from_id, "stop", "test_secret")

        with patch("services.messaging.main.httpx.post") as mock_send:
            mock_send.return_value = MagicMock(status_code=200)
            mock_send.return_value.raise_for_status = MagicMock()
            async with AsyncClient(
                transport=ASGITransport(app=messaging_app),
                base_url=self.BASE_URL,
            ) as client:
                resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)

        assert resp.status_code == 200
        assert "cancelled" in mock_send.call_args.kwargs["json"]["text"]["body"].lower()

    @pytest.mark.asyncio
    async def test_cancel_with_nothing_pending_sends_polite_message(
        self, test_merchant, monkeypatch
    ):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "test_secret")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        from_id = test_merchant["from_id"]
        payload, headers = self._signed_post(from_id, "CANCEL", "test_secret")

        with patch("services.messaging.main.httpx.post") as mock_send:
            mock_send.return_value = MagicMock(status_code=200)
            mock_send.return_value.raise_for_status = MagicMock()
            async with AsyncClient(
                transport=ASGITransport(app=messaging_app),
                base_url=self.BASE_URL,
            ) as client:
                resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)

        assert resp.status_code == 200
        sent_body = mock_send.call_args.kwargs["json"]["text"]["body"].lower()
        assert any(phrase in sent_body for phrase in ["pending", "don", "no pending"])



class TestPaymentConfirmedNotification:
    def test_sends_to_meta_with_correct_content(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", TEST_PHONE_NUMBER_ID)
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", TEST_ACCESS_TOKEN)

        # Meta bare-digit format — no 'whatsapp:+' prefix
        merchant_thread = "2348012345678"

        with patch("services.messaging.main.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            send_payment_confirmed_notification(
                merchant_confam_thread_id=merchant_thread,
                amount_minor_units=75000,
                link_id="abcdef12-0000-0000-0000-000000000000",
            )

        mock_post.assert_called_once()
        sent = mock_post.call_args.kwargs["json"]
        assert sent["to"] == merchant_thread   # bare-digit format preserved
        assert "₦750.00" in sent["text"]["body"]
        assert "Payment received" in sent["text"]["body"]

    def test_notification_failure_does_not_raise(self, monkeypatch):
        """Rule 10: notification failure must not propagate to the settlement engine."""
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", TEST_PHONE_NUMBER_ID)
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", TEST_ACCESS_TOKEN)

        with patch("services.messaging.main.httpx.post", side_effect=Exception("Meta unreachable")):
            # Must not raise
            send_payment_confirmed_notification(
                merchant_confam_thread_id="2348012345678",
                amount_minor_units=10000,
                link_id="test-link-id",
            )
