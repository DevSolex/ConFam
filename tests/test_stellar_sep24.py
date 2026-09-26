"""
tests/test_stellar_sep24.py

SEP-24 scaffold tests against the public Stellar test anchor.

IMPORTANT: these tests make real network calls to testanchor.stellar.org.
They are marked with `stellar_live` so they can be excluded from offline
CI runs: `pytest -m "not stellar_live"`.

Test names are deliberately conservative — they assert protocol mechanics,
not that a real payment succeeded:
  test_sep24_web_auth_against_test_anchor
  test_sep24_withdraw_request_against_test_anchor
  test_sep24_transaction_status_poll_against_test_anchor

Nothing here implies:
  - A real NGN payout
  - Lobstr compatibility (see OQ-023)
  - A production-ready integration
  - Any specific anchor vendor (see OQ-024)

Open questions this scaffold does NOT resolve:
  OQ-023 — Lobstr/SEP-24 compatibility
  OQ-024 — Production anchor selection
"""

import pytest
from stellar_sdk import Keypair, Network

from rails.stellar.sep24 import (
    Sep24Error,
    Sep24TransactionStatus,
    Sep24WithdrawResponse,
    TEST_ANCHOR_AUTH,
    TEST_ANCHOR_SEP24,
    get_sep10_jwt,
    initiate_sep24_withdraw,
    poll_sep24_transaction,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_keypair() -> Keypair:
    """
    Ephemeral testnet keypair. Generated fresh per test module run.
    Never persisted. Never funded with real XLM.
    Engineering Rule 8: no signing keys hardcoded or stored.
    """
    return Keypair.random()


@pytest.fixture(scope="module")
def sep10_jwt(test_keypair) -> str:
    """JWT from SEP-10 web auth — reused across tests in this module."""
    return get_sep10_jwt(test_keypair, anchor_auth_url=TEST_ANCHOR_AUTH)


# ---------------------------------------------------------------------------
# SEP-10 Web Authentication
# ---------------------------------------------------------------------------

@pytest.mark.stellar_live
class TestSep10WebAuth:
    def test_sep10_web_auth_against_test_anchor(self, test_keypair):
        """
        Confirms SEP-10 authentication against the public test anchor succeeds
        and returns a non-empty JWT string.

        Scope: protocol mechanics only. Does not move any funds.
        """
        jwt = get_sep10_jwt(test_keypair, anchor_auth_url=TEST_ANCHOR_AUTH)
        assert isinstance(jwt, str)
        assert len(jwt) > 10
        # JWTs are base64url-encoded and contain exactly two dots
        assert jwt.count(".") == 2, f"Expected JWT format, got: {jwt[:40]}"


# ---------------------------------------------------------------------------
# SEP-24 Withdraw initiation
# ---------------------------------------------------------------------------

@pytest.mark.stellar_live
class TestSep24WithdrawInitiation:
    def test_sep24_withdraw_request_against_test_anchor(self, test_keypair, sep10_jwt):
        """
        Confirms that initiating a SEP-24 withdraw against the public test anchor
        returns a parseable response with an interactive URL and transaction ID.

        Scope: proves the initiation mechanics work. The interactive_url is
        what a buyer would open — this test does NOT open it, complete KYC,
        or initiate a transfer. No funds move.

        NOTE: Whether a Lobstr user can be handed this URL from an external
        checkout page is unknown — see OQ-023.
        """
        result = initiate_sep24_withdraw(
            jwt=sep10_jwt,
            account=test_keypair.public_key,
            asset_code="USDC",
            amount="1",
            sep24_base_url=TEST_ANCHOR_SEP24,
        )

        assert isinstance(result, Sep24WithdrawResponse)
        assert result.transaction_id, "transaction_id must be non-empty"
        assert result.interactive_url.startswith("https://"), (
            f"Expected HTTPS interactive URL, got: {result.interactive_url[:60]}"
        )
        assert result.response_type == "interactive_customer_info_needed", (
            f"Expected SEP-24 interactive response type, got: {result.response_type}"
        )

    def test_sep24_withdraw_returns_different_ids_per_request(self, test_keypair, sep10_jwt):
        """
        Two separate withdraw initiations must return different transaction IDs.
        Confirms the anchor creates a new transaction per request.
        """
        r1 = initiate_sep24_withdraw(
            jwt=sep10_jwt, account=test_keypair.public_key,
            asset_code="USDC", amount="1", sep24_base_url=TEST_ANCHOR_SEP24,
        )
        r2 = initiate_sep24_withdraw(
            jwt=sep10_jwt, account=test_keypair.public_key,
            asset_code="USDC", amount="1", sep24_base_url=TEST_ANCHOR_SEP24,
        )
        assert r1.transaction_id != r2.transaction_id


# ---------------------------------------------------------------------------
# SEP-24 Transaction status polling
# ---------------------------------------------------------------------------

@pytest.mark.stellar_live
class TestSep24StatusPolling:
    def test_sep24_transaction_status_poll_against_test_anchor(self, test_keypair, sep10_jwt):
        """
        Initiates a withdraw and immediately polls its status. Confirms the
        status poll returns a parseable SEP-24 transaction object.

        Expected status immediately after initiation: "incomplete"
        (buyer has not yet opened the interactive URL and completed KYC).

        Scope: proves the polling mechanics work. Does not wait for completion.
        """
        # Initiate to get a real transaction_id
        result = initiate_sep24_withdraw(
            jwt=sep10_jwt, account=test_keypair.public_key,
            asset_code="USDC", amount="1", sep24_base_url=TEST_ANCHOR_SEP24,
        )

        # Poll status
        status = poll_sep24_transaction(
            jwt=sep10_jwt,
            transaction_id=result.transaction_id,
            sep24_base_url=TEST_ANCHOR_SEP24,
        )

        assert isinstance(status, Sep24TransactionStatus)
        assert status.transaction_id == result.transaction_id
        assert status.kind == "withdrawal"
        assert status.status in (
            "incomplete",
            "pending_user_transfer_start",
            "pending_anchor",
            "pending_stellar",
            "completed",
            "error",
            "expired",
        ), f"Unexpected status: {status.status}"

    def test_sep24_poll_invalid_id_raises(self, sep10_jwt):
        """Polling a non-existent transaction ID must raise Sep24Error."""
        with pytest.raises(Sep24Error):
            poll_sep24_transaction(
                jwt=sep10_jwt,
                transaction_id="00000000-0000-0000-0000-000000000000",
                sep24_base_url=TEST_ANCHOR_SEP24,
            )
