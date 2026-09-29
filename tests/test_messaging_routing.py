"""
tests/test_messaging_routing.py

State-aware WhatsApp command routing.

The reported production bug: a merchant who had just REGISTERed (and was
therefore status=pending_verification) was told "Your ConFam account is not yet
active. Please complete account setup first." when they sent ONBOARD — the very
command that completes setup. A blanket `status != 'active'` gate ran before
command dispatch, so ONBOARD was unreachable for every merchant who had not
already onboarded.

Coverage:
  - The exact reported transcript, as a regression test.
  - Table-driven matrix of merchant state x command.
  - End-to-end REGISTER -> ONBOARD (Paystack mocked) -> PAY.
  - ONBOARD hardening: resolved name + masked number, no plaintext account
    numbers in logs, friendly Paystack failure/rate-limit replies,
    per-sender rate limiting.

Paystack and the Meta send API are always mocked — no real API calls.
DB writes go through the app's own confam_app pool, so the tests also prove the
grants required by these paths are actually in place.
"""

import hashlib
import hmac
import json
import os
import uuid
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from httpx import ASGITransport, AsyncClient

from services.messaging.main import app as messaging_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_APP_SECRET = "test_meta_app_secret_routing"
TEST_PHONE_NUMBER_ID = "123456789012345"
TEST_ACCESS_TOKEN = "test_meta_access_token"
BASE_URL = "http://test"

# A valid Paystack test bank code, and a 10-digit account number.
ACCOUNT_NUMBER = "7042698747"
BANK_CODE = "999992"
RESOLVED_NAME = "SOLEX SHOES LTD"


@pytest.fixture(autouse=True)
def no_real_paystack_calls():
    """Fail loudly if a test forgets to mock a Paystack call.

    confam.paystack does its own httpx calls, separate from the Meta send that
    _send patches, so an un-mocked path would silently hit the live API.
    """
    from confam import paystack

    def _boom(*args, **kwargs):
        raise AssertionError("real Paystack HTTP call attempted in tests")

    with patch.object(paystack.httpx, "get", _boom), \
            patch.object(paystack.httpx, "post", _boom):
        yield


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
def messaging_env(monkeypatch):
    """Env the webhook handler needs, pointed at the test database."""
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", TEST_PHONE_NUMBER_ID)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", TEST_ACCESS_TOKEN)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify_token_routing")
    monkeypatch.setenv("DATABASE_URL", test_url)
    monkeypatch.setenv("CHECKOUT_BASE_URL", "http://pay.confam.co")


def _signed_payload(from_id: str, text: str) -> tuple[bytes, dict]:
    payload = json.dumps({
        "entry": [{"changes": [{"value": {"messages": [
            {"from": from_id, "type": "text", "text": {"body": text}}
        ]}}]}]
    }).encode()
    sig = "sha256=" + hmac.new(
        TEST_APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return payload, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


async def _send(from_id: str, text: str) -> str:
    """Deliver one inbound message and return the reply body. '' if no reply."""
    payload, headers = _signed_payload(from_id, text)
    with patch("services.messaging.main.httpx.post") as mock_send:
        mock_send.return_value = MagicMock(status_code=200)
        mock_send.return_value.raise_for_status = MagicMock()
        async with AsyncClient(
            transport=ASGITransport(app=messaging_app), base_url=BASE_URL  # type: ignore[arg-type]
        ) as client:
            resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)
        assert resp.status_code == 200
        if not mock_send.call_args_list:
            return ""
        return mock_send.call_args_list[-1].kwargs["json"]["text"]["body"]


def _mock_paystack(name: str = RESOLVED_NAME, subaccount_code: str = "ACCT_test_123"):
    """Patch the two Paystack calls ONBOARD makes. The handlers import them
    lazily from confam.paystack, so the source module is the patch target."""
    from confam.paystack import CreatedSubaccount, ResolvedAccount

    return (
        patch("confam.paystack.resolve_bank_account",
              return_value=ResolvedAccount(
                  account_number=ACCOUNT_NUMBER, account_name=name, bank_id=1,
              )),
        patch("confam.paystack.create_subaccount",
              return_value=CreatedSubaccount(
                  subaccount_code=subaccount_code, business_name="solex shoes",
              )),
    )


def _merchant(status: str, migrator_conn, *, with_account: bool = False) -> dict:
    """Insert a merchant in `status`, optionally with an active payout account."""
    run_id = uuid.uuid4().hex[:12]
    from_id = f"234700{run_id}"
    with migrator_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO merchants (whatsapp_number, confam_thread_id, business_name, status)
               VALUES (%s, %s, 'solex shoes', %s) RETURNING merchant_id""",
            (f"+{from_id}", from_id, status),
        )
        merchant_id = str(cur.fetchone()[0])
        if with_account:
            cur.execute(
                """INSERT INTO payout_accounts (
                       merchant_id, bank_account_number, bank_code, account_holder_name,
                       verification_method, verified_at, active_from,
                       paystack_subaccount_code)
                   VALUES (%s, 'enc', 'enc', %s, 'bank_api_resolve', now(), now(), %s)""",
                (merchant_id, RESOLVED_NAME, f"ACCT_{run_id}"),
            )
    migrator_conn.commit()
    return {"merchant_id": merchant_id, "from_id": from_id}


def _payout_account_count(migrator_conn, merchant_id: str) -> int:
    with migrator_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM payout_accounts WHERE merchant_id = %s", (merchant_id,)
        )
        return cur.fetchone()[0]


def _merchant_status(migrator_conn, merchant_id: str) -> str:
    with migrator_conn.cursor() as cur:
        cur.execute("SELECT status FROM merchants WHERE merchant_id = %s", (merchant_id,))
        return cur.fetchone()[0]


@pytest.fixture()
def sender(migrator_conn):
    """A unique, unregistered sender id, cleaned up afterwards.

    The webhook path creates real rows, so the id must be unique per run or a
    second run of the suite would resolve it as an existing merchant.
    """
    from_id = "2347" + uuid.uuid4().hex[:12]
    yield from_id
    with migrator_conn.cursor() as cur:
        cur.execute(
            "SELECT merchant_id FROM merchants WHERE confam_thread_id = %s", (from_id,)
        )
        row = cur.fetchone()
        if row:
            merchant_id = str(row[0])
            cur.execute(
                "DELETE FROM payment_links WHERE merchant_id = %s", (merchant_id,)
            )
            cur.execute(
                "DELETE FROM payout_accounts WHERE merchant_id = %s", (merchant_id,)
            )
            cur.execute("DELETE FROM merchants WHERE merchant_id = %s", (merchant_id,))
    migrator_conn.commit()


# ---------------------------------------------------------------------------
# Regression: the exact reported transcript
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestReportedTranscriptRegression:
    """Reproduces the production transcript from the bug report verbatim.

    Pre-fix, step 3 answered "Your ConFam account is not yet active. Please
    complete account setup first." and step 4 answered the same, and no payout
    account was ever created — so a merchant could not onboard at all.
    """

    @pytest.mark.asyncio
    async def test_hi_register_onboard_pay(self, messaging_env, migrator_conn, sender):
        from_id = sender

        resolve_mock, subaccount_mock = _mock_paystack()
        with resolve_mock, subaccount_mock:
            # 1. "Hi" from an unknown sender -> registration instructions
            step1 = await _send(from_id, "Hi")
            assert "REGISTER" in step1, step1

            # 2. "Register solex shoes" -> merchant created as pending_verification
            step2 = await _send(from_id, "Register solex shoes")
            assert "registered" in step2.lower(), step2
            assert "ONBOARD" in step2, step2

            with migrator_conn.cursor() as cur:
                cur.execute(
                    "SELECT merchant_id, status FROM merchants WHERE confam_thread_id = %s",
                    (from_id,),
                )
                row = cur.fetchone()
            assert row is not None
            merchant_id = str(row[0])
            assert row[1] == "pending_verification"

            # 3. "Onboard 7042698747 999992" -> must run the onboarding flow.
            #    This is the assertion that failed in production.
            step3 = await _send(from_id, "Onboard 7042698747 999992")
            assert "not yet active" not in step3.lower(), f"regression: {step3}"
            assert "complete account setup" not in step3.lower(), f"regression: {step3}"
            assert _merchant_status(migrator_conn, merchant_id) == "active"
            assert _payout_account_count(migrator_conn, merchant_id) == 1

            # 4. "Pay 1233" -> no longer blocked by the status gate. It has no
            #    description, so the parser answers with usage — what matters is
            #    that it is no longer the "not yet active" wall.
            step4 = await _send(from_id, "Pay 1233")
            assert "not yet active" not in step4.lower(), f"regression: {step4}"

            # 4b. A well-formed PAY now returns a checkout link.
            step5 = await _send(from_id, "PAY 1233 Ankara fabric x2")
            assert "pay.confam.co" in step5, step5
            assert "₦1,233.00" in step5, step5


# ---------------------------------------------------------------------------
# State x command matrix
# ---------------------------------------------------------------------------

ONBOARD_MSG = f"ONBOARD {ACCOUNT_NUMBER} {BANK_CODE}"
PAY_MSG = "PAY 750 Ankara fabric x2"

# (id, merchant status, command, must_contain, must_not_contain)
MATRIX: list[tuple[str, str | None, str, list[str], list[str]]] = [
    # -- unknown sender -------------------------------------------------
    ("unknown_greeting", None, "Hi", ["REGISTER"], []),
    ("unknown_pay", None, PAY_MSG, ["REGISTER"], []),
    ("unknown_onboard", None, ONBOARD_MSG, ["REGISTER"], []),

    # -- pending_verification -------------------------------------------
    ("pending_onboard", "pending_verification", ONBOARD_MSG, ["Payout account set up"], []),
    ("pending_pay", "pending_verification", PAY_MSG,
     ["Finish setup first", "ONBOARD <account number> <bank code>"], []),
    ("pending_register", "pending_verification", "REGISTER again",
     ["already registered", "ONBOARD"], []),
    ("pending_update", "pending_verification", f"UPDATE {ACCOUNT_NUMBER} {BANK_CODE}",
     ["don't have a payout account"], []),
    ("pending_cancel", "pending_verification", "CANCEL", ["don't have a pending"], []),
    ("pending_garbage", "pending_verification", "what can you do", ["ONBOARD"], []),
    ("pending_onboard_lowercase", "pending_verification",
     f"  onboard   {ACCOUNT_NUMBER}   {BANK_CODE}  ", ["Payout account set up"], []),

    # -- active ---------------------------------------------------------
    ("active_pay", "active", PAY_MSG, ["pay.confam.co"], []),
    ("active_onboard", "active", ONBOARD_MSG,
     ["already set", "separate process"], []),
    ("active_register", "active", "REGISTER again", ["already registered"], []),
    ("active_garbage", "active", "what can you do", ["PAY"], []),

    # -- suspended / unknown status -------------------------------------
    ("suspended_pay", "suspended", PAY_MSG, ["contact support"], []),
    ("suspended_onboard", "suspended", ONBOARD_MSG, ["contact support"], []),
    ("suspended_register", "suspended", "REGISTER again", ["contact support"], []),
    ("suspended_garbage", "suspended", "hello", ["contact support"], []),
]


@pytest.mark.integration
class TestCommandMatrix:
    """Every cell of the merchant state x command matrix."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case,status,command,must_contain,must_not_contain",
        MATRIX,
        ids=[case[0] for case in MATRIX],
    )
    async def test_cell(
        self, messaging_env, migrator_conn, sender,
        case, status, command, must_contain, must_not_contain,
    ):
        if status is None:
            target = {"merchant_id": None, "from_id": sender}
        else:
            target = _merchant(status, migrator_conn, with_account=(status == "active"))

        resolve_mock, subaccount_mock = _mock_paystack()
        with resolve_mock, subaccount_mock:
            reply = await _send(target["from_id"], command)

        for expected in must_contain:
            assert expected.lower() in reply.lower(), f"{case}: {reply!r}"

        for forbidden in must_not_contain:
            assert forbidden.lower() not in reply.lower(), f"{case}: {reply!r}"

        # The dead-end message from the bug must never come back.
        assert "not yet active" not in reply.lower(), f"{case}: {reply!r}"

    @pytest.mark.asyncio
    async def test_unknown_sender_register_creates_pending(
        self, messaging_env, migrator_conn, sender
    ):
        reply = await _send(sender, "Register solex shoes")
        assert "registered" in reply.lower()
        with migrator_conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM merchants WHERE confam_thread_id = %s", (sender,)
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "pending_verification"

    @pytest.mark.asyncio
    async def test_active_onboard_creates_no_second_account(
        self, messaging_env, migrator_conn
    ):
        """ONBOARD from an onboarded merchant must not write to payout_accounts.

        The unique index that used to prevent this was dropped in migration 010,
        so the guard is the routing plus the handler's own re-check.
        """
        merchant = _merchant("active", migrator_conn, with_account=True)
        before = _payout_account_count(migrator_conn, merchant["merchant_id"])

        resolve_mock, subaccount_mock = _mock_paystack()
        with resolve_mock, subaccount_mock as subaccount:
            reply = await _send(merchant["from_id"], ONBOARD_MSG)

        assert "already set" in reply.lower()
        # Must not have reached Paystack at all
        subaccount.assert_not_called()
        after = _payout_account_count(migrator_conn, merchant["merchant_id"])
        assert after == before == 1

    @pytest.mark.asyncio
    async def test_second_onboard_after_success_is_refused(
        self, messaging_env, migrator_conn, sender
    ):
        """After a successful ONBOARD the merchant is active; a repeat must not
        create a second payout account."""
        resolve_mock, subaccount_mock = _mock_paystack()
        with resolve_mock, subaccount_mock:
            await _send(sender, "REGISTER solex shoes")
            first = await _send(sender, ONBOARD_MSG)
            assert "set up" in first.lower()

            with migrator_conn.cursor() as cur:
                cur.execute(
                    "SELECT merchant_id FROM merchants WHERE confam_thread_id = %s", (sender,)
                )
                merchant_id = str(cur.fetchone()[0])

            second = await _send(sender, ONBOARD_MSG)
            assert "already set" in second.lower()
            assert _payout_account_count(migrator_conn, merchant_id) == 1


# ---------------------------------------------------------------------------
# End to end: REGISTER -> ONBOARD -> PAY
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestEndToEndOnboarding:
    @pytest.mark.asyncio
    async def test_register_onboard_pay(
        self, messaging_env, migrator_conn, sender
    ):
        """The full merchant journey, with Paystack mocked."""
        resolve_mock, subaccount_mock = _mock_paystack()

        with resolve_mock as resolve, subaccount_mock as subaccount:
            registered = await _send(sender, "REGISTER solex shoes")
            assert "registered" in registered.lower()
            assert "ONBOARD" in registered

            onboarded = await _send(sender, f"onboard {ACCOUNT_NUMBER} {BANK_CODE}")
            assert "Payout account set up" in onboarded
            resolve.assert_called_once_with(account_number=ACCOUNT_NUMBER, bank_code=BANK_CODE)
            subaccount.assert_called_once()

            paid = await _send(sender, "PAY 750 Ankara fabric x2")

        assert "pay.confam.co" in paid
        assert "₦750.00" in paid
        assert "Ankara fabric x2" in paid

        with migrator_conn.cursor() as cur:
            cur.execute(
                "SELECT merchant_id, status FROM merchants WHERE confam_thread_id = %s",
                (sender,),
            )
            row = cur.fetchone()
            assert row is not None, "merchant row not found after ONBOARD"
            merchant_id, status = str(row[0]), row[1]
            cur.execute(
                """SELECT account_holder_name, bank_code, verification_method,
                          paystack_subaccount_code, active_from
                   FROM payout_accounts WHERE merchant_id = %s""",
                (merchant_id,),
            )
            row = cur.fetchone()
            cur.execute(
                "SELECT amount_minor_units, status FROM payment_links WHERE merchant_id = %s",
                (merchant_id,),
            )
            link = cur.fetchone()

        assert status == "active"
        # The bank-verified name is stored for audit (Rule 9)
        assert row[0] == RESOLVED_NAME
        assert row[1] == BANK_CODE
        assert row[2] == "bank_api_resolve"
        assert row[3] == "ACCT_test_123"
        assert row[4] is not None
        assert link == (75000, "created")


# ---------------------------------------------------------------------------
# ONBOARD hardening
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestOnboardHardening:
    @pytest.mark.asyncio
    async def test_reply_shows_resolved_name_and_masked_number(
        self, messaging_env, migrator_conn, sender
    ):
        """Hardening 1: the merchant must see who the account resolved to."""
        resolve_mock, subaccount_mock = _mock_paystack()
        with resolve_mock, subaccount_mock:
            await _send(sender, "REGISTER solex shoes")
            reply = await _send(sender, ONBOARD_MSG)

        assert RESOLVED_NAME in reply
        assert ACCOUNT_NUMBER[-4:] in reply
        # Only the last 4 digits; the rest is masked
        assert ACCOUNT_NUMBER[:-4] not in reply
        assert "*" * 6 in reply

    @pytest.mark.asyncio
    async def test_account_number_not_logged_in_plaintext(
        self, messaging_env, migrator_conn, sender, capsys
    ):
        """Hardening 2: account numbers typed in chat must not reach the logs.

        The reply is allowed to show the masked number — it goes to the
        merchant's own thread. The structured logs must not.
        """
        resolve_mock, subaccount_mock = _mock_paystack()
        with resolve_mock, subaccount_mock:
            await _send(sender, "REGISTER solex shoes")
            await _send(sender, ONBOARD_MSG)

        blob = capsys.readouterr().out
        assert "onboard_command_succeeded" in blob, "expected log output to inspect"
        assert ACCOUNT_NUMBER not in blob, f"account number leaked to logs:\n{blob}"
        # Sanity check: the capture is real, not empty
        assert "merchant_registered_via_whatsapp" in blob

    @pytest.mark.asyncio
    async def test_paystack_rate_limit_gives_friendly_retry_message(
        self, messaging_env, migrator_conn, sender
    ):
        """Hardening 3: Paystack throttling must not surface as a generic error."""
        from confam.paystack import PaystackRateLimited

        with patch("confam.paystack.resolve_bank_account",
                   side_effect=PaystackRateLimited("HTTP 429")):
            await _send(sender, "REGISTER solex shoes")
            reply = await _send(sender, ONBOARD_MSG)

        assert "couldn't verify" in reply.lower()
        assert "try again later" in reply.lower()
        assert "429" not in reply
        # And no account was written
        with migrator_conn.cursor() as cur:
            cur.execute("SELECT status FROM merchants WHERE confam_thread_id = %s", (sender,))
            assert cur.fetchone()[0] == "pending_verification"

    @pytest.mark.asyncio
    async def test_paystack_422_asks_for_corrected_details(
        self, messaging_env, migrator_conn, sender
    ):
        """A rejected account tells the merchant to fix their input."""
        from confam.paystack import PaystackError

        with patch("confam.paystack.resolve_bank_account",
                   side_effect=PaystackError("Paystack bank/resolve failed: HTTP 422")):
            await _send(sender, "REGISTER solex shoes")
            reply = await _send(sender, ONBOARD_MSG)

        assert "couldn't verify" in reply.lower()
        assert "check the 10-digit account number" in reply.lower()
        # Internal error text must not be echoed to the merchant
        assert "422" not in reply
        assert "Paystack" not in reply

    @pytest.mark.asyncio
    async def test_per_sender_onboard_rate_limit(
        self, messaging_env, migrator_conn, sender, monkeypatch
    ):
        """Hardening 4: ONBOARD attempts are limited per sender.

        Paystack resolve is mocked to fail, so the merchant stays pending and
        every ONBOARD reaches the limiter. A second sender has its own budget.
        """
        from confam.paystack import PaystackError
        from services.messaging import main as messaging_main

        monkeypatch.setenv("RATE_LIMIT_ONBOARD_PER_HOUR", "2")
        messaging_main._RESOLVE_ATTEMPTS.clear()

        resolve_patch, subaccount_patch = _mock_paystack()
        with resolve_patch as resolve:
            resolve.side_effect = PaystackError("Paystack bank/resolve failed: HTTP 422")
            with subaccount_patch:
                await _send(sender, "REGISTER solex shoes")

                first = await _send(sender, ONBOARD_MSG)
                second = await _send(sender, ONBOARD_MSG)
                third = await _send(sender, ONBOARD_MSG)

        assert "couldn't verify" in first.lower()
        assert "couldn't verify" in second.lower()
        assert "several account verification attempts" in third.lower()
        # The limit stopped the third attempt before it reached Paystack
        assert resolve.call_count == 2

        # A different sender is unaffected
        other = _merchant("pending_verification", migrator_conn)
        resolve_patch2, subaccount_patch2 = _mock_paystack()
        with resolve_patch2 as resolve2:
            resolve2.side_effect = PaystackError("Paystack bank/resolve failed: HTTP 422")
            with subaccount_patch2:
                reply = await _send(other["from_id"], ONBOARD_MSG)
        assert "several account verification attempts" not in reply.lower()
        assert "couldn't verify" in reply.lower()

        messaging_main._RESOLVE_ATTEMPTS.clear()

    def test_rate_limit_disabled_by_env(self, monkeypatch):
        """A limit of 0 turns throttling off."""
        from services.messaging import main as messaging_main

        monkeypatch.setenv("RATE_LIMIT_ONBOARD_PER_HOUR", "0")
        messaging_main._RESOLVE_ATTEMPTS.clear()
        assert all(
            messaging_main.check_resolve_rate_limit("2347000000000") is False
            for _ in range(50)
        )
        messaging_main._RESOLVE_ATTEMPTS.clear()
