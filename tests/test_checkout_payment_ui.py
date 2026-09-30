"""
tests/test_checkout_payment_ui.py

Tests for the buyer-facing checkout: method -> Paystack channel mapping, email
validation, the link-status poll endpoint, and the two money-path cases the
buyer UI newly makes possible — a buyer paying the same link twice, and a
payment that clears after the link expired.

Rules under test:
  Rule 1  — the payout whitelist check still runs before ANY Paystack call
  Rule 2  — a duplicate payment writes a RailEvent but never a second sale
  Rule 6  — amounts stay int (kobo) end to end
  Rule 7  — a duplicate payment is an incident (Sentry), not a silent retry
  Rule 11 — no double count: one link, one sale, always

No test here calls Paystack. confam.paystack's httpx is stubbed out for the
whole module, so an un-mocked path fails loudly instead of hitting the live API.
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

from services.checkout.main import app as checkout_app
from services.checkout.pay import METHOD_CHANNELS
from services.settlement_engine.main import app as engine_app

WEBHOOK_SECRET = "test_checkout_webhook_secret"


# ---------------------------------------------------------------------------
# Module-wide guard: no test may reach Paystack over the network
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_live_paystack():
    from confam import paystack

    def _boom(*args, **kwargs):
        raise AssertionError("a test attempted a real Paystack HTTP call")

    with patch.object(paystack.httpx, "get", _boom), \
            patch.object(paystack.httpx, "post", _boom):
        yield


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


@pytest.fixture()
def merchant(migrator_conn) -> dict:
    with migrator_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO merchants
                (whatsapp_number, confam_thread_id, status, business_name)
            VALUES (%s, %s, 'active', %s)
            RETURNING merchant_id
            """,
            (
                f"+234-000-{uuid.uuid4().hex[:8]}",
                f"checkout-thread-{uuid.uuid4()}",
                "Adaeze Fashion Store",
            ),
        )
        merchant_id = str(cur.fetchone()[0])
    migrator_conn.commit()
    return {"merchant_id": merchant_id, "business_name": "Adaeze Fashion Store"}


@pytest.fixture()
def payout_account(migrator_conn, merchant) -> dict:
    with migrator_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO payout_accounts (
                merchant_id, bank_account_number, bank_code,
                verification_method, verified_at, active_from,
                paystack_subaccount_code
            )
            VALUES (%s, 'enc-test-acct', '058', 'micro_deposit', now(),
                    now() - interval '1 minute', %s)
            RETURNING payout_account_id
            """,
            (merchant["merchant_id"], "ACCT_test_placeholder_code"),
        )
        payout_account_id = str(cur.fetchone()[0])
    migrator_conn.commit()
    return {
        "payout_account_id": payout_account_id,
        "merchant_id": merchant["merchant_id"],
        "subaccount_code": "ACCT_test_placeholder_code",
    }


def _make_link(db_conn, merchant_id: str, *, expires_in_seconds: int = 1800,
               status: str = "opened", amount: int = 250000,
               description: str = "Ankara fabric x2") -> str:
    """Insert a link directly. Returns its link_id."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO payment_links
                (merchant_id, amount_minor_units, currency, description,
                 status, expires_at)
            VALUES (%s, %s, 'NGN', %s, %s,
                    now() + make_interval(secs => %s))
            RETURNING link_id
            """,
            (merchant_id, amount, description, status, expires_in_seconds),
        )
        link_id = str(cur.fetchone()[0])
    db_conn.commit()
    return link_id


@pytest.fixture()
def link(db_conn, merchant) -> str:
    return _make_link(db_conn, merchant["merchant_id"])


def _mock_tx(reference: str = "ref_mock") -> MagicMock:
    tx = MagicMock()
    tx.authorization_url = "https://checkout.paystack.com/mock_access_code"
    tx.access_code = "mock_access_code"
    tx.reference = reference
    return tx


@pytest.fixture(autouse=True)
def _point_app_at_test_db(monkeypatch):
    """
    Endpoints in the checkout/settlement apps use confam.db.get_conn(), which
    reads DATABASE_URL. Point it at the test role whenever one is configured so
    the app endpoints and the test fixtures see the same database, and close the
    pool after each test so DATABASE_URL can change.
    """
    url = os.environ.get("TEST_DATABASE_URL", "")
    if url:
        monkeypatch.setenv("DATABASE_URL", url)
    yield
    import confam.db as _db

    _db.close_pool()


# ---------------------------------------------------------------------------
# 1. method -> Paystack channels
# ---------------------------------------------------------------------------

class TestMethodChannelMapping:
    """The buyer picks a method; we send Paystack a channel. The mapping is the
    whole contract, so it is asserted end to end — through the HTTP endpoint
    down to the payload handed to Paystack."""

    EXPECTED = {
        "card": ["card"],
        "bank_transfer": ["bank_transfer"],
        "bank": ["bank"],
        "ussd": ["ussd"],
    }

    def test_map_covers_exactly_the_four_offered_methods(self):
        # Guards against a method being added to METHOD_CHANNELS with no test
        # saying what channel it should send.
        assert set(METHOD_CHANNELS) == set(self.EXPECTED)

    def test_opay_is_part_of_the_bank_channel_not_its_own(self):
        # Paystack has no "opay" channel. OPay appears as an option inside
        # Pay-with-Bank on the hosted page. If someone ever adds a separate
        # "opay" method, Paystack would silently ignore it and show whatever
        # else the account has enabled.
        assert "opay" not in METHOD_CHANNELS
        assert METHOD_CHANNELS["bank"] == ["bank"]

    def test_no_stellar_channel(self):
        # Stellar is a ConFam-side rail (rail_events.rail = 'stellar'), not a
        # Paystack channel. OQ-023/OQ-024 are still open.
        assert all("stellar" not in ch for chs in METHOD_CHANNELS.values() for ch in chs)

    @pytest.mark.integration
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,expected_channels", sorted(EXPECTED.items()))
    async def test_each_method_sends_the_right_channels(
        self, db_conn, link, payout_account, monkeypatch, method, expected_channels
    ):
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")

        with patch(
            "services.checkout.pay.initialize_transaction", return_value=_mock_tx()
        ) as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{link}/pay",
                    json={"method": method, "email": "buyer@example.com"},
                )

        assert resp.status_code == 200, resp.text
        assert resp.json()["authorization_url"] == "https://checkout.paystack.com/mock_access_code"
        # The payload itself — not just the response.
        assert init.call_args.kwargs["channels"] == expected_channels

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_callback_url_returns_the_buyer_to_this_link(
        self, db_conn, link, payout_account, monkeypatch
    ):
        """Paystack must redirect the buyer back to THIS link's page, not to
        whatever URL is configured on the dashboard for some other integration."""
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")
        monkeypatch.setenv("CHECKOUT_BASE_URL", "https://confam.example.com/pay")

        with patch(
            "services.checkout.pay.initialize_transaction", return_value=_mock_tx()
        ) as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{link}/pay",
                    json={"method": "card", "email": "buyer@example.com"},
                )
            assert resp.status_code == 200
            assert init.call_args.kwargs["callback_url"] == (
                f"https://confam.example.com/pay/{link}"
            )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_callback_url_falls_back_to_request_host_without_env(
        self, db_conn, link, payout_account, monkeypatch
    ):
        """Local dev has no CHECKOUT_BASE_URL; the request host is the only
        sensible answer. Crucially it is the request, never the request BODY —
        a buyer-supplied callback_url would let anyone bounce Paystack to a
        hostile page."""
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")
        monkeypatch.delenv("CHECKOUT_BASE_URL", raising=False)

        with patch(
            "services.checkout.pay.initialize_transaction", return_value=_mock_tx()
        ) as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://localhost:8001"
            ) as client:
                resp = await client.post(
                    f"/{link}/pay",
                    json={"method": "card", "email": "buyer@example.com"},
                )
            assert resp.status_code == 200
            assert init.call_args.kwargs["callback_url"] == f"http://localhost:8001/{link}"


# ---------------------------------------------------------------------------
# 2. Rejecting bad input
# ---------------------------------------------------------------------------

class TestInputRejection:
    @pytest.mark.integration
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_method", ["crypto", "mobile_money", "OPAY", "", "card;drop"])
    async def test_unknown_method_is_422_and_never_calls_paystack(
        self, db_conn, link, payout_account, monkeypatch, bad_method
    ):
        """A typo must not silently fall back to another method. If 'cardr'
        quietly became bank_transfer, the buyer would be sent a transfer
        request for a payment they asked to make by card."""
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")

        with patch("services.checkout.pay.initialize_transaction") as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{link}/pay",
                    json={"method": bad_method, "email": "buyer@example.com"},
                )

        assert resp.status_code == 422, resp.text
        init.assert_not_called()

    @pytest.mark.integration
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_email",
        ["", "   ", "notanemail", "missing@domain", "@nolocal.com", "two@@at.com", "a b@c.com"],
    )
    async def test_invalid_email_is_422_and_never_calls_paystack(
        self, db_conn, link, payout_account, monkeypatch, bad_email
    ):
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")

        with patch("services.checkout.pay.initialize_transaction") as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{link}/pay",
                    json={"method": "card", "email": bad_email},
                )

        assert resp.status_code == 422, resp.text
        init.assert_not_called()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_email_is_trimmed_before_use(
        self, db_conn, link, payout_account, monkeypatch
    ):
        """A trailing space from a phone keyboard must not become a Paystack
        rejection the buyer sees as an unexplained failure."""
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")

        with patch(
            "services.checkout.pay.initialize_transaction", return_value=_mock_tx()
        ) as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{link}/pay",
                    json={"method": "card", "email": "  buyer@example.com  "},
                )

        assert resp.status_code == 200, resp.text
        assert init.call_args.kwargs["email"] == "buyer@example.com"

    def test_unknown_channel_is_rejected_before_the_api_call(self):
        """Paystack ignores unrecognised channel names and shows everything the
        account has enabled instead. That would quietly widen what a buyer can
        pay with, so the client refuses rather than trusting the API."""
        from confam.paystack import initialize_transaction

        with pytest.raises(ValueError, match="Unknown Paystack channel"):
            initialize_transaction(
                amount_minor_units=1000,
                email="buyer@example.com",
                subaccount_code="ACCT_x",
                reference="ref",
                link_id="00000000-0000-0000-0000-000000000000",
                channels=["card", "carrier_pigeon"],
            )

    def test_empty_channels_is_rejected(self):
        from confam.paystack import initialize_transaction

        with pytest.raises(ValueError, match="at least one channel"):
            initialize_transaction(
                amount_minor_units=1000,
                email="buyer@example.com",
                subaccount_code="ACCT_x",
                reference="ref",
                link_id="00000000-0000-0000-0000-000000000000",
                channels=[],
            )


# ---------------------------------------------------------------------------
# 3. Rule 1 still holds, and still runs first
# ---------------------------------------------------------------------------

class TestWhitelistCheckStillRunsFirst:
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_no_payout_account_is_503_and_paystack_is_never_called(
        self, db_conn, merchant, monkeypatch
    ):
        """The merchant with no active payout account must fail BEFORE Paystack
        is asked to create a transaction. If we called Paystack first we would
        have created a live transaction with no whitelisted destination — a real
        order for money we have no legal route to move."""
        link_id = _make_link(db_conn, merchant["merchant_id"])
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")

        with patch("services.checkout.pay.initialize_transaction") as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{link_id}/pay",
                    json={"method": "card", "email": "buyer@example.com"},
                )

        assert resp.status_code == 503
        init.assert_not_called()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_no_subaccount_code_is_503_and_paystack_is_never_called(
        self, db_conn, migrator_conn, merchant, monkeypatch
    ):
        """Same rule, narrower case: an account with no Paystack subaccount
        cannot receive a split, so we must not open a transaction against it."""
        with migrator_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payout_accounts (
                    merchant_id, bank_account_number, bank_code,
                    verification_method, verified_at, active_from
                )
                VALUES (%s, 'enc-no-sub', '058', 'micro_deposit', now(),
                        now() - interval '1 minute')
                """,
                (merchant["merchant_id"],),
            )
        migrator_conn.commit()

        link_id = _make_link(db_conn, merchant["merchant_id"])
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")

        with patch("services.checkout.pay.initialize_transaction") as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{link_id}/pay",
                    json={"method": "card", "email": "buyer@example.com"},
                )

        assert resp.status_code == 503
        init.assert_not_called()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_subaccount_code_comes_from_the_whitelist_not_the_request(
        self, db_conn, link, payout_account, monkeypatch
    ):
        """Rule 1: the subaccount is read from the whitelisted PayoutAccount.
        A buyer cannot name a destination."""
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")

        with patch(
            "services.checkout.pay.initialize_transaction", return_value=_mock_tx()
        ) as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{link}/pay",
                    json={
                        "method": "card",
                        "email": "buyer@example.com",
                        # All of these must be ignored, not honoured.
                        "subaccount": "ACCT_attacker",
                        "amount_minor_units": 1,
                        "reference": "attacker-ref",
                    },
                )

        assert resp.status_code == 200
        assert init.call_args.kwargs["subaccount_code"] == payout_account["subaccount_code"]
        assert init.call_args.kwargs["amount_minor_units"] == 250000
        assert init.call_args.kwargs["reference"] == link


# ---------------------------------------------------------------------------
# 4. Link status endpoint
# ---------------------------------------------------------------------------

class TestLinkStatusEndpoint:
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_fresh_link_is_waiting(self, db_conn, link, merchant):
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{link}/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "waiting"
        assert body["merchant_name"] == merchant["business_name"]
        assert body["amount_minor_units"] == 250000

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_logged_link_reports_logged_not_410(
        self, db_conn, link, migrator_conn
    ):
        """The buyer returns here straight after paying. If the status endpoint
        410'd on a logged link it would be useless exactly when it matters."""
        with migrator_conn.cursor() as cur:
            cur.execute("UPDATE payment_links SET status = 'logged' WHERE link_id = %s", (link,))
        migrator_conn.commit()

        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{link}/status")

        assert resp.status_code == 200
        assert resp.json()["state"] == "logged"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_failed_link_reports_failed(self, db_conn, link, migrator_conn):
        with migrator_conn.cursor() as cur:
            cur.execute("UPDATE payment_links SET status = 'failed' WHERE link_id = %s", (link,))
        migrator_conn.commit()

        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{link}/status")

        assert resp.json()["state"] == "failed"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_expired_link_reports_expired(self, db_conn, merchant):
        link_id = _make_link(db_conn, merchant["merchant_id"], expires_in_seconds=-60)

        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{link_id}/status")

        assert resp.status_code == 200
        assert resp.json()["state"] == "expired"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_logged_wins_over_expired(self, db_conn, merchant, migrator_conn):
        """Money arrived after the deadline (slow bank transfer). Telling the
        buyer 'expired' when their money landed is both wrong and the fastest
        route to a chargeback."""
        link_id = _make_link(db_conn, merchant["merchant_id"], expires_in_seconds=-60)
        with migrator_conn.cursor() as cur:
            cur.execute("UPDATE payment_links SET status = 'logged' WHERE link_id = %s", (link_id,))
        migrator_conn.commit()

        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{link_id}/status")

        assert resp.json()["state"] == "logged"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_unknown_link_is_404(self):
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{uuid.uuid4()}/status")

        assert resp.status_code == 404

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_status_does_not_transition_the_link(self, db_conn, link):
        """Polling must not be able to 'open' a link. If it could, a buyer
        holding a link_id could drive link state with a GET."""
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            await client.get(f"/{link}/status")

        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM payment_links WHERE link_id = %s", (link,))
            assert cur.fetchone()[0] == "opened"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_status_does_not_leak_internals(self, db_conn, link, payout_account):
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{link}/status")

        body = resp.json()
        # No merchant id, no payout account, no subaccount code, no Paystack ref.
        assert "merchant_id" not in body
        assert "payout_account" not in body
        assert "subaccount" not in body
        assert payout_account["subaccount_code"] not in resp.text


# ---------------------------------------------------------------------------
# 5. The checkout page itself
# ---------------------------------------------------------------------------

class TestCheckoutPageHtml:
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_page_offers_all_four_methods(self, db_conn, link):
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{link}")

        assert resp.status_code == 200
        html = resp.text
        for method in ("card", "bank_transfer", "bank", "ussd"):
            assert f'data-method="{method}"' in html, f"missing button for {method}"
        assert "Pay with card" in html
        assert "Pay with bank transfer" in html
        assert "Pay with bank / OPay" in html
        assert "Pay with USSD" in html

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_page_has_no_coming_soon_and_no_stellar_promises(
        self, db_conn, link
    ):
        """The old page promised bank transfer and Lobstr/Stellar were "coming
        soon". One is now real; the other is not coming until OQ-023/OQ-024
        close, so neither placeholder belongs on a page a buyer trusts."""
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            html = (await client.get(f"/{link}")).text

        lowered = html.lower()
        assert "coming soon" not in lowered
        for word in ("stellar", "lobstr", "lumens", "xlm"):
            assert word not in lowered, f"{word} still on the checkout page"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_page_requires_an_email_marked_for_the_receipt(self, db_conn, link):
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            html = (await client.get(f"/{link}")).text

        assert 'type="email"' in html
        assert "required" in html
        assert "for your receipt" in html.lower()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_page_contains_no_card_input_fields(self, db_conn, link):
        """PCI boundary. Card details are entered on Paystack's page only. If
        anyone ever adds a card field here, ConFam is in PCI scope — this test
        is the tripwire."""
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            html = (await client.get(f"/{link}")).text

        for forbidden in ('name="card_number"', 'name="cvv"', 'name="cvc"',
                          'name="expiry"', 'name="cardnumber"', 'autocomplete="cc-"'):
            assert forbidden not in html, f"card input on ConFam's page: {forbidden}"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_page_shows_merchant_amount_and_description(
        self, db_conn, link, merchant
    ):
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            html = (await client.get(f"/{link}")).text

        assert merchant["business_name"] in html
        assert "₦2,500" in html          # 250000 kobo, formatted without floats
        assert "Ankara fabric x2" in html

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_page_polls_and_never_trusts_the_callback_params(
        self, db_conn, link
    ):
        """The page must poll our status endpoint and must NOT read the
        reference/trxref Paystack appends to the callback URL. Those are
        round-trippable by anyone holding the link."""
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            html = (await client.get(f"/{link}")).text

        assert "/status" in html
        assert "3000" in html or "3 * 1000" in html   # poll interval
        # It may notice that Paystack redirected, but must not read the values.
        assert ".get(\"reference\")" not in html
        assert ".get(\"trxref\")" not in html
        assert "Payment received" in html

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_expired_link_page_shows_expired_not_a_410(self, db_conn, merchant):
        """A buyer who opened the link too late gets an explanation, not a bare
        HTTP error page."""
        link_id = _make_link(db_conn, merchant["merchant_id"], expires_in_seconds=-60)

        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{link_id}")

        assert resp.status_code == 200
        assert "expired" in resp.text.lower()
        # The bootstrap tells the JS it is not payable, so the form is hidden
        # and the terminal state rendered instead.
        assert '"payable": false' in resp.text
        assert '"state": "expired"' in resp.text

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_logged_link_page_shows_received_not_a_410(self, db_conn, link, migrator_conn):
        """This is the post-payment landing page. It must not 410."""
        with migrator_conn.cursor() as cur:
            cur.execute("UPDATE payment_links SET status = 'logged' WHERE link_id = %s", (link,))
        migrator_conn.commit()

        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{link}")

        assert resp.status_code == 200
        assert "Payment received" in resp.text

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_unknown_link_page_is_404(self):
        async with AsyncClient(
            transport=ASGITransport(app=checkout_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/{uuid.uuid4()}")

        assert resp.status_code == 404

    def test_amount_formatting_never_uses_floats(self):
        """Rule 6 holds for the string a buyer reads. 123456789 kobo must not
        round-trip through a float."""
        from services.checkout.main import _format_amount

        assert _format_amount(1000, "NGN") == "₦10"
        assert _format_amount(250000, "NGN") == "₦2,500"
        assert _format_amount(250050, "NGN") == "₦2,500.50"
        assert _format_amount(123456789, "NGN") == "₦1,234,567.89"

    def test_ghanaian_cedi_is_shown_with_its_own_symbol(self):
        """A Ghana merchant's link is GHS. Rendering it without a symbol (the
        pre-Ghana behaviour) shows the buyer a bare number and no idea which
        currency they are about to be charged in."""
        from services.checkout.main import _format_amount

        assert _format_amount(250000, "GHS") == "GH₵2,500"
        assert _format_amount(250050, "GHS") == "GH₵2,500.50"

    def test_ussd_is_not_offered_on_a_cedi_link(self):
        """USSD is a Paystack Nigeria channel. A Ghanaian buyer shown it can
        only fail, so the page must not offer it."""
        from services.checkout.main import _offered_methods

        ngn = [method for method, _label, _hint in _offered_methods("NGN")]
        assert ngn == ["card", "bank_transfer", "bank", "ussd"]

        ghs = [method for method, _label, _hint in _offered_methods("GHS")]
        assert ghs == ["card", "bank_transfer", "bank"]

    def test_every_offered_method_is_a_channel_the_server_accepts(self):
        """A button naming a method the pay endpoint rejects is a dead end for
        the buyer. The page and the server must offer the same set."""
        from services.checkout.main import _offered_methods

        for currency in ("NGN", "GHS"):
            offered = {method for method, _l, _h in _offered_methods(currency)}
            assert offered <= set(METHOD_CHANNELS)


# ---------------------------------------------------------------------------
# 6. Unified app mount — the route the buyer actually hits on Render
# ---------------------------------------------------------------------------

class TestUnifiedAppMount:
    """The standalone checkout app and the unified app mount the same handlers
    at different prefixes. A buyer on Render only ever hits /pay/{link_id}, so
    that path needs its own coverage."""

    @pytest.fixture()
    def unified_app(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("ADMIN_API_KEY", "test_admin_key")
        import importlib

        import app as unified

        return importlib.reload(unified).app

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_status_route_is_mounted_under_pay(self, db_conn, link, unified_app):
        async with AsyncClient(
            transport=ASGITransport(app=unified_app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/pay/{link}/status")

        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "waiting"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_pay_route_is_mounted_under_pay(
        self, db_conn, link, payout_account, unified_app, monkeypatch
    ):
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")
        monkeypatch.setenv("CHECKOUT_BASE_URL", "https://confam.example.com/pay")

        with patch(
            "services.checkout.pay.initialize_transaction", return_value=_mock_tx()
        ) as init:
            async with AsyncClient(
                transport=ASGITransport(app=unified_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/pay/{link}/pay",
                    json={"method": "card", "email": "buyer@example.com"},
                )

        assert resp.status_code == 200, resp.text
        assert init.call_args.kwargs["channels"] == ["card"]
        assert init.call_args.kwargs["callback_url"] == f"https://confam.example.com/pay/{link}"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_legacy_pay_bank_route_still_works(
        self, db_conn, link, payout_account, monkeypatch
    ):
        """Kept so nothing that already calls /pay/bank breaks. It defaults to
        bank_transfer, which is what the endpoint did before methods existed."""
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")

        with patch(
            "services.checkout.pay.initialize_transaction", return_value=_mock_tx()
        ) as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{link}/pay/bank",
                    json={"email": "buyer@example.com"},
                )

        assert resp.status_code == 200, resp.text
        assert init.call_args.kwargs["channels"] == ["bank_transfer"]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_page_uses_the_mounted_prefix_for_its_api_calls(
        self, db_conn, link, unified_app
    ):
        """The page has to call back into the same mount it was served from, or
        the buyer's payment POST 404s."""
        async with AsyncClient(
            transport=ASGITransport(app=unified_app), base_url="http://test"
        ) as client:
            html = (await client.get(f"/pay/{link}")).text

        assert f"/pay/{link}" in html


# ---------------------------------------------------------------------------
# 7. Duplicate payment — the buyer paid the same link twice
# ---------------------------------------------------------------------------

def _charge_success(link_id: str, reference: str, subaccount_code: str,
                    amount: int = 250000) -> dict:
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


async def _post_charge_success(payload: dict) -> int:
    body = json.dumps(payload).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha512).hexdigest()
    async with AsyncClient(
        transport=ASGITransport(app=engine_app), base_url="http://test"  # type: ignore[arg-type]
    ) as client:
        resp = await client.post(
            "/webhooks/paystack",
            content=body,
            headers={"Content-Type": "application/json", "x-paystack-signature": sig},
        )
    return resp.status_code


@pytest.mark.integration
class TestDuplicatePayment:
    """A buyer can start a card payment, not finish it, and then pay by
    transfer. Two distinct references, one link, two payments taken. That is
    money ConFam is holding twice and owes back once."""

    @pytest.mark.asyncio
    @pytest.mark.no_double_count
    async def test_second_payment_records_a_duplicate_and_no_second_sale(
        self, db_conn, link, payout_account, monkeypatch
    ):
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", WEBHOOK_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        first_ref = f"ref-card-{uuid.uuid4()}"
        second_ref = f"ref-transfer-{uuid.uuid4()}"

        assert await _post_charge_success(
            _charge_success(link, first_ref, payout_account["subaccount_code"])
        ) == 200

        with patch("sentry_sdk.capture_message") as sentry:
            assert await _post_charge_success(
                _charge_success(link, second_ref, payout_account["subaccount_code"])
            ) == 200

        # The surplus payment is recorded, with a disposition that says so.
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT disposition, processed FROM rail_events WHERE rail_reference = %s",
                (second_ref,),
            )
            row = cur.fetchone()
            assert row is not None, "the duplicate payment was not recorded at all"
            assert row[0] == "duplicate"
            # TRUE = reviewed and deliberately not applied. FALSE would make the
            # reconciliation job retry it forever.
            assert row[1] is True

        # ...and crucially, still exactly ONE sale for this link.
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE link_id = %s AND entry_type = 'sale'",
                (link,),
            )
            assert cur.fetchone()[0] == 1, "a second sale was written for one link"

        # The original payment's event is untouched.
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT disposition, processed FROM rail_events WHERE rail_reference = %s",
                (first_ref,),
            )
            assert cur.fetchone() == ("applied", True)

        # Rule 7: a refund is owed, so a human must be told.
        sentry.assert_called_once()
        message = sentry.call_args.args[0]
        assert link in message
        assert "REFUND" in message.upper() or "refund" in message
        assert sentry.call_args.kwargs.get("level") == "error"

    @pytest.mark.asyncio
    async def test_link_stays_logged_and_is_not_reprocessed(
        self, db_conn, link, payout_account, monkeypatch
    ):
        """The duplicate must not disturb the link's terminal state or re-notify
        the merchant about a second sale."""
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", WEBHOOK_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        await _post_charge_success(
            _charge_success(link, f"ref-a-{uuid.uuid4()}", payout_account["subaccount_code"])
        )
        with patch("sentry_sdk.capture_message"):
            await _post_charge_success(
                _charge_success(link, f"ref-b-{uuid.uuid4()}", payout_account["subaccount_code"])
            )

        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM payment_links WHERE link_id = %s", (link,))
            assert cur.fetchone()[0] == "logged"

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM rail_events WHERE link_id = %s", (link,)
            )
            assert cur.fetchone()[0] == 2, "expected both the sale and the duplicate to be recorded"

    @pytest.mark.asyncio
    async def test_retried_duplicate_event_does_not_insert_twice(
        self, db_conn, link, payout_account, monkeypatch
    ):
        """Paystack retries. The duplicate is already recorded, so a retry must
        hit the UNIQUE (rail, rail_reference) index and no-op — not raise a
        second Sentry alert for the same money."""
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", WEBHOOK_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        await _post_charge_success(
            _charge_success(link, f"ref-a-{uuid.uuid4()}", payout_account["subaccount_code"])
        )
        duplicate = _charge_success(
            link, f"ref-dup-{uuid.uuid4()}", payout_account["subaccount_code"]
        )

        with patch("sentry_sdk.capture_message") as sentry:
            assert await _post_charge_success(duplicate) == 200
            assert await _post_charge_success(duplicate) == 200
        sentry.assert_called_once()

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM rail_events WHERE rail_reference = %s",
                (duplicate["data"]["reference"],),
            )
            assert cur.fetchone()[0] == 1

    @pytest.mark.asyncio
    async def test_payment_that_cannot_be_routed_marks_the_link_failed(
        self, db_conn, migrator_conn, merchant, monkeypatch
        # No active payout account -> the payment cannot be routed. The link is
        # marked failed (Rule 10's failure policy), so a human sees it.
    ):
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", WEBHOOK_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        orphan_link = _make_link(db_conn, merchant["merchant_id"])

        await _post_charge_success(
            _charge_success(orphan_link, f"ref-x-{uuid.uuid4()}", "ACCT_ghost")
        )

        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM payment_links WHERE link_id = %s", (orphan_link,))
            assert cur.fetchone()[0] == "failed"


# ---------------------------------------------------------------------------
# 8. Payment that clears after the link expired
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestPaymentAfterLinkExpiry:
    @pytest.mark.asyncio
    async def test_late_payment_is_recorded_as_a_sale_and_flagged(
        self, db_conn, merchant, payout_account, monkeypatch
    ):
        """
        What used to happen: nothing at all. There was no expiry check in the
        handler, so a payment that cleared after the 30-minute window was
        logged as an ordinary sale with no trace that it was late.

        What happens now: same correct outcome (the sale is written — refusing
        money already collected would strand the buyer and contradict the
        ledger), plus a durable 'applied_after_expiry' flag so slow settlements
        on expired links are visible instead of surprising.
        """
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", WEBHOOK_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        expired_link = _make_link(
            db_conn, merchant["merchant_id"], expires_in_seconds=-120
        )
        reference = f"ref-late-{uuid.uuid4()}"

        assert await _post_charge_success(
            _charge_success(expired_link, reference, payout_account["subaccount_code"])
        ) == 200

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT disposition, processed FROM rail_events WHERE rail_reference = %s",
                (reference,),
            )
            row = cur.fetchone()
            assert row is not None, "the late payment was dropped entirely"
            assert row[0] == "applied_after_expiry"
            assert row[1] is True

            cur.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE link_id = %s AND entry_type = 'sale'",
                (expired_link,),
            )
            assert cur.fetchone()[0] == 1, "money that arrived must be recorded as a sale"

            cur.execute("SELECT status FROM payment_links WHERE link_id = %s", (expired_link,))
            assert cur.fetchone()[0] == "logged"

    @pytest.mark.asyncio
    async def test_ontime_payment_is_not_flagged_as_late(
        self, db_conn, link, payout_account, monkeypatch
    ):
        """The common case must stay clean, or the flag is noise and gets ignored."""
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", WEBHOOK_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        reference = f"ref-ontime-{uuid.uuid4()}"
        await _post_charge_success(
            _charge_success(link, reference, payout_account["subaccount_code"])
        )

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT disposition FROM rail_events WHERE rail_reference = %s",
                (reference,),
            )
            assert cur.fetchone()[0] == "applied"

    @pytest.mark.asyncio
    async def test_late_payment_does_not_raise_sentry(
        self, db_conn, merchant, payout_account, monkeypatch
        # no refund is due, so this is not an incident
    ):
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", WEBHOOK_SECRET)
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

        expired_link = _make_link(
            db_conn, merchant["merchant_id"], expires_in_seconds=-120
        )
        with patch("sentry_sdk.capture_message") as sentry:
            await _post_charge_success(
                _charge_success(
                    expired_link, f"ref-late2-{uuid.uuid4()}",
                    payout_account["subaccount_code"],
                )
            )
        sentry.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_link_cannot_be_paid_again_from_the_buyer_side(
        self, db_conn, merchant, payout_account, monkeypatch
    ):
        """The late-payment path exists because a payment was STARTED inside the
        window. Starting a new one after expiry must still be refused — otherwise
        the 'applied_after_expiry' flag would become the normal case."""
        monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test_secret")

        expired_link = _make_link(
            db_conn, merchant["merchant_id"], expires_in_seconds=-60
        )

        with patch("services.checkout.pay.initialize_transaction") as init:
            async with AsyncClient(
                transport=ASGITransport(app=checkout_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/{expired_link}/pay",
                    json={"method": "card", "email": "buyer@example.com"},
                )

        assert resp.status_code == 410
        init.assert_not_called()
