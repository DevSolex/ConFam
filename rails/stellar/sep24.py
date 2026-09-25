"""
rails/stellar/sep24.py — SEP-24 scaffold against the public Stellar test anchor.

PURPOSE: Prove the SEP-24 mechanics work in ConFam's codebase.
  - Web authentication (SEP-10): get a challenge, sign with a Stellar keypair,
    exchange for a JWT.
  - Withdraw initiation: call /sep24/transactions/withdraw/interactive,
    get back an interactive URL and transaction ID.
  - Status polling: call /sep24/transaction to check transaction state.

SCOPE LIMITS — do not expand this beyond what is here:
  - No Lobstr integration (OQ-023: compatibility unknown, needs testing).
  - No production NGN anchor (OQ-024: vendor selection pending).
  - No real money movement. Tests use the public testnet anchor only.
  - No checkout-page "pay with Stellar" button (premature until OQ-023 resolved).

The test anchor used here: https://testanchor.stellar.org
Supported assets: SRT, USDC (test), native XLM
TRANSFER_SERVER_SEP0024: https://testanchor.stellar.org/sep24
WEB_AUTH_ENDPOINT: https://testanchor.stellar.org/auth

Protocol correction (from previous task): SEP-31 was incorrect for this flow.
SEP-24 is correct because buyers hold funds in self-custody wallets (Lobstr)
and must interact with an anchor-hosted UI to initiate the transfer.
SEP-31 is non-interactive institution-to-institution — not applicable here.
See OQ-005-R, OQ-023, OQ-024 in OPEN_QUESTIONS.md.

Engineering Rule 8: no signing keys or credentials are hardcoded here.
Test keypairs are generated ephemerally — never stored.
"""

import os
from dataclasses import dataclass

import httpx
from stellar_sdk import Keypair, Network, TransactionEnvelope

import structlog

log = structlog.get_logger()

# Public Stellar test anchor — safe to hardcode; this is a public test service.
TEST_ANCHOR_BASE = "https://testanchor.stellar.org"
TEST_ANCHOR_SEP24 = f"{TEST_ANCHOR_BASE}/sep24"
TEST_ANCHOR_AUTH = f"{TEST_ANCHOR_BASE}/auth"
TEST_NETWORK_PASSPHRASE = Network.TESTNET_NETWORK_PASSPHRASE


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Sep24WithdrawResponse:
    """
    Result of a successful SEP-24 withdraw initiation.

    type: "interactive_customer_info_needed" — the buyer must open `url`
          to complete KYC and authorize the transfer.
    url: the anchor-hosted interactive URL for the buyer.
    transaction_id: used for status polling.

    NOTE: `url` is what would be shown to the buyer on the checkout page.
    Whether this can be opened from an external checkout (vs. only from
    inside Lobstr) is unknown — see OQ-023.
    """
    transaction_id: str
    interactive_url: str
    response_type: str   # always "interactive_customer_info_needed" for SEP-24


@dataclass(frozen=True)
class Sep24TransactionStatus:
    """Result of a status poll against the anchor."""
    transaction_id: str
    kind: str           # "withdrawal" or "deposit"
    status: str         # "incomplete", "pending_user_transfer_start", "completed", etc.
    more_info_url: str | None


class Sep24Error(Exception):
    """Raised on any SEP-24 protocol or network error."""


# ---------------------------------------------------------------------------
# SEP-10 Web Authentication
# ---------------------------------------------------------------------------

def get_sep10_jwt(keypair: Keypair, anchor_auth_url: str = TEST_ANCHOR_AUTH) -> str:
    """
    Perform SEP-10 web authentication and return a JWT.

    1. GET /auth?account=<public_key> — anchor returns a challenge transaction.
    2. Sign the challenge with the keypair.
    3. POST /auth with the signed transaction — anchor returns a JWT.

    The JWT is then used as Bearer auth on SEP-24 endpoints.

    Engineering Rule 8: the keypair must be generated ephemerally by the caller
    and never persisted. In production, the keypair would be ConFam's own
    Stellar account keypair, stored in AWS Secrets Manager.
    """
    try:
        # Step 1: get challenge
        resp = httpx.get(anchor_auth_url, params={"account": keypair.public_key}, timeout=15.0)
        resp.raise_for_status()
        challenge_xdr = resp.json()["transaction"]

        # Step 2: sign challenge
        te = TransactionEnvelope.from_xdr(challenge_xdr, network_passphrase=TEST_NETWORK_PASSPHRASE)
        te.sign(keypair)

        # Step 3: submit signed transaction
        token_resp = httpx.post(anchor_auth_url, json={"transaction": te.to_xdr()}, timeout=15.0)
        token_resp.raise_for_status()
        jwt = token_resp.json()["token"]

    except httpx.TimeoutException as exc:
        raise Sep24Error(f"SEP-10 auth timed out against {anchor_auth_url}") from exc
    except (httpx.HTTPStatusError, KeyError) as exc:
        raise Sep24Error(f"SEP-10 auth failed: {exc}") from exc

    log.info("sep10_auth_success", anchor=anchor_auth_url, account=keypair.public_key[:8] + "...")
    return jwt


# ---------------------------------------------------------------------------
# SEP-24 Withdraw initiation
# ---------------------------------------------------------------------------

def initiate_sep24_withdraw(
    jwt: str,
    account: str,
    asset_code: str = "USDC",
    amount: str = "1",
    sep24_base_url: str = TEST_ANCHOR_SEP24,
) -> Sep24WithdrawResponse:
    """
    Initiate a SEP-24 interactive withdrawal.

    Returns an interactive URL the buyer would open to complete the transfer.

    Parameters:
        jwt: from get_sep10_jwt()
        account: the Stellar account public key making the withdrawal request
        asset_code: "USDC", "SRT", or "native" (test anchor supports all three)
        amount: amount to withdraw (string, in asset units)
        sep24_base_url: the anchor's TRANSFER_SERVER_SEP0024

    NOTE: This does NOT move any money. It only initiates the interactive flow.
    The buyer must open interactive_url and complete KYC/transfer there.
    Whether a Lobstr user can do this from an external checkout page is
    unknown — see OQ-023.
    """
    try:
        resp = httpx.post(
            f"{sep24_base_url}/transactions/withdraw/interactive",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Content-Type": "application/json",
            },
            json={"asset_code": asset_code, "amount": amount, "account": account},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException as exc:
        raise Sep24Error("SEP-24 withdraw initiation timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise Sep24Error(f"SEP-24 withdraw failed: {exc.response.status_code} {exc.response.text[:200]}") from exc

    response_type = data.get("type", "")
    interactive_url = data.get("url", "")
    transaction_id = data.get("id", "")

    if not transaction_id or not interactive_url:
        raise Sep24Error(f"SEP-24 withdraw response missing required fields: {data}")

    log.info(
        "sep24_withdraw_initiated",
        transaction_id=transaction_id,
        asset_code=asset_code,
        amount=amount,
        response_type=response_type,
    )

    return Sep24WithdrawResponse(
        transaction_id=transaction_id,
        interactive_url=interactive_url,
        response_type=response_type,
    )


# ---------------------------------------------------------------------------
# SEP-24 Transaction status polling
# ---------------------------------------------------------------------------

def poll_sep24_transaction(
    jwt: str,
    transaction_id: str,
    sep24_base_url: str = TEST_ANCHOR_SEP24,
) -> Sep24TransactionStatus:
    """
    Poll the anchor for a SEP-24 transaction's current status.

    The settlement engine would poll this (or listen for Stellar ledger events)
    to know when a buyer's withdrawal is complete and funds are in transit.

    Status values from the SEP-24 spec:
      incomplete, pending_user_transfer_start, pending_anchor,
      pending_stellar, pending_external, completed, error, expired

    NOTE: This scaffold does not implement automated polling or event-driven
    detection. A production implementation would use a background worker
    (the existing SQS worker pattern) to poll on a schedule.
    """
    try:
        resp = httpx.get(
            f"{sep24_base_url}/transaction",
            params={"id": transaction_id},
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        txn = resp.json()["transaction"]
    except httpx.TimeoutException as exc:
        raise Sep24Error("SEP-24 transaction poll timed out") from exc
    except (httpx.HTTPStatusError, KeyError) as exc:
        raise Sep24Error(f"SEP-24 transaction poll failed: {exc}") from exc

    return Sep24TransactionStatus(
        transaction_id=txn.get("id", transaction_id),
        kind=txn.get("kind", ""),
        status=txn.get("status", ""),
        more_info_url=txn.get("more_info_url"),
    )
