"""
confam.paystack — Paystack API client and webhook verification.

Responsibilities:
  - initialize_transaction(): call Paystack's Initialize Transaction API,
    passing the merchant's subaccount code from the whitelisted PayoutAccount.
  - verify_webhook_signature(): HMAC-SHA512 verification of inbound webhooks.

What this module does NOT do:
  - It does not decide where money goes (that's the whitelist lookup in
    confam.payout_accounts, which happens before this module is called).
  - It does not write to the database.
  - It does not contain any business logic beyond making the API call and
    verifying signatures.

Engineering Rule 1: the subaccount_code passed to initialize_transaction()
must come from a confam.payout_accounts.PayoutAccount read from the whitelist,
never from user input or runtime construction.

Engineering Rule 8: PAYSTACK_SECRET_KEY is read from environment, never logged.

Paystack Split Payment model (verified against docs.paystack.com):
  - The buyer pays the full amount on Paystack's hosted checkout page.
  - Paystack's settlement engine splits the funds: the merchant's share goes
    to their bank account per their settlement_schedule (default: AUTO = next
    business day), ConFam's platform share stays in ConFam's Paystack balance.
  - ConFam never initiates a separate transfer and never holds buyer funds in
    transit — the split routing is inside Paystack's own settlement engine.
  - The charge.success webhook fires when the buyer's payment is collected,
    NOT when the merchant's bank account is credited (settlement happens later
    on Paystack's schedule). The ledger entry records "payment confirmed by
    Paystack," not "merchant's bank account credited."
"""

import hashlib
import hmac
import os
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

PAYSTACK_API_BASE = os.environ.get("PAYSTACK_BASE_URL", "https://api.paystack.co")


class PaystackError(Exception):
    """Raised when the Paystack API returns an error or is unreachable."""


class WebhookSignatureInvalid(Exception):
    """
    Raised when the x-paystack-signature header does not match the payload.
    An unverified webhook must never be processed (Rule 1's security boundary).
    """


@dataclass(frozen=True)
class InitializedTransaction:
    """Result of a successful Initialize Transaction API call."""
    authorization_url: str   # Redirect the buyer here
    access_code: str
    reference: str           # Paystack's transaction reference — used as rail_reference


@dataclass(frozen=True)
class ResolvedAccount:
    """Result of a successful bank/resolve API call."""
    account_number: str
    account_name: str    # Bank-verified account holder name
    bank_id: int


@dataclass(frozen=True)
class CreatedSubaccount:
    """Result of a successful Create Subaccount API call."""
    subaccount_code: str   # e.g. ACCT_xxxxxxxxxx — stored on PayoutAccount
    business_name: str


def _secret_key() -> str:
    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    if not key:
        raise PaystackError(
            "PAYSTACK_SECRET_KEY is not set. "
            "Set it in .env (test mode) or AWS Secrets Manager (production). "
            "Engineering Rule 8: never hardcode this value."
        )
    return key


def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> None:
    """
    Verify the x-paystack-signature HMAC-SHA512 header.

    Paystack signs the raw request body with your secret key using HMAC-SHA512.
    This must be called before trusting anything in the webhook payload.

    Raises WebhookSignatureInvalid if the signature does not match.
    Raises PaystackError if PAYSTACK_SECRET_KEY is not set.

    This is not optional hardening — it is the entire security boundary of
    the webhook endpoint. A fabricated "charge.success" event from a bad actor
    would cause ConFam to write a fraudulent LedgerEntry.
    """
    expected = hmac.new(
        _secret_key().encode("utf-8"),
        payload_bytes,
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        log.warning("paystack_webhook_signature_invalid")
        raise WebhookSignatureInvalid(
            "x-paystack-signature header does not match payload. "
            "Request rejected."
        )


def initialize_transaction(
    *,
    amount_minor_units: int,
    email: str,
    subaccount_code: str,
    reference: str,
    link_id: str,
    transaction_charge_minor_units: int = 0,
) -> InitializedTransaction:
    """
    Call Paystack's Initialize Transaction API and return the authorization URL.

    Parameters:
        amount_minor_units: total amount in kobo (Rule 6 — always int).
        email: buyer's email address (required by Paystack).
        subaccount_code: the merchant's Paystack subaccount code, read from
            the whitelisted PayoutAccount (Rule 1 — never constructed at runtime).
        reference: a unique reference for this transaction (we use the link_id).
        link_id: embedded in metadata so the webhook handler can match the
            Paystack reference back to the ConFam PaymentLink.
        transaction_charge_minor_units: ConFam's platform fee in kobo (flat fee
            that stays in ConFam's Paystack balance). The remainder goes to the
            merchant's subaccount. 0 means full amount goes to the subaccount.

    Raises PaystackError on API failure or unreachability.

    NOTE on settlement timing: charge.success fires when the buyer's payment
    is collected. The merchant's bank account is credited later, per their
    settlement_schedule (default AUTO = next business day). The LedgerEntry
    written on charge.success records payment confirmation, not bank credit.
    """
    if not isinstance(amount_minor_units, int) or isinstance(amount_minor_units, bool):
        raise TypeError(
            f"amount_minor_units must be int (kobo), got {type(amount_minor_units).__name__}. "
            "Engineering Rule 6."
        )

    payload: dict = {
        "amount": amount_minor_units,
        "email": email,
        "reference": reference,
        "subaccount": subaccount_code,
        "metadata": {
            "confam_link_id": link_id,
            "cancel_action": "payment_cancelled",
        },
    }
    if transaction_charge_minor_units > 0:
        payload["transaction_charge"] = transaction_charge_minor_units

    try:
        response = httpx.post(
            f"{PAYSTACK_API_BASE}/transaction/initialize",
            headers={
                "Authorization": f"Bearer {_secret_key()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException as exc:
        raise PaystackError(
            f"Paystack Initialize Transaction timed out for link {link_id}. "
            "Check Rule 10 failure policy in rails/bank/README.md."
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise PaystackError(
            f"Paystack Initialize Transaction failed: {exc.response.status_code} "
            f"{exc.response.text[:200]}"
        ) from exc

    if not data.get("status"):
        raise PaystackError(
            f"Paystack Initialize Transaction returned status=false: {data.get('message')}"
        )

    tx = data["data"]
    return InitializedTransaction(
        authorization_url=tx["authorization_url"],
        access_code=tx["access_code"],
        reference=tx["reference"],
    )


def resolve_bank_account(
    *,
    account_number: str,
    bank_code: str,
) -> ResolvedAccount:
    """
    Call Paystack's GET /bank/resolve to verify a bank account exists and
    retrieve the account holder's name.

    Used during merchant payout account onboarding to confirm the submitted
    bank details are valid before creating a subaccount.

    Raises PaystackError if the account cannot be resolved (invalid details,
    API error, or unreachable).

    Rule 9: the returned account_name is stored for audit purposes. It is not
    automatically matched against the merchant's claimed business name at this
    verification tier — that is a future KYB-depth feature.
    """
    try:
        response = httpx.get(
            f"{PAYSTACK_API_BASE}/bank/resolve",
            headers={"Authorization": f"Bearer {_secret_key()}"},
            params={"account_number": account_number, "bank_code": bank_code},
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException as exc:
        raise PaystackError(
            f"Paystack bank/resolve timed out for account {account_number[:4]}****."
        ) from exc
    except httpx.HTTPStatusError as exc:
        # 422 from Paystack means the account number / bank code combination is invalid.
        raise PaystackError(
            f"Paystack bank/resolve failed: {exc.response.status_code} "
            f"{exc.response.text[:200]}"
        ) from exc

    if not data.get("status"):
        raise PaystackError(
            f"Paystack bank/resolve returned status=false: {data.get('message')}"
        )

    acct = data["data"]
    return ResolvedAccount(
        account_number=acct["account_number"],
        account_name=acct["account_name"],
        bank_id=acct.get("bank_id", 0),
    )


def create_subaccount(
    *,
    business_name: str,
    bank_code: str,
    account_number: str,
    percentage_charge: float = 0.0,
) -> CreatedSubaccount:
    """
    Call Paystack's POST /subaccount to create a subaccount for a merchant.

    The subaccount_code returned is stored on the merchant's PayoutAccount row
    and used for all future split-payment initializations (Rule 1).

    percentage_charge: the share of each transaction that goes to ConFam's
    main account (e.g. 1.0 = 1%). The remainder goes to the merchant's
    subaccount. Currently 0 (no platform fee taken at this stage).

    Raises PaystackError on API failure.
    """
    try:
        response = httpx.post(
            f"{PAYSTACK_API_BASE}/subaccount",
            headers={
                "Authorization": f"Bearer {_secret_key()}",
                "Content-Type": "application/json",
            },
            json={
                "business_name": business_name,
                "bank_code": bank_code,
                "account_number": account_number,
                "percentage_charge": percentage_charge,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException as exc:
        raise PaystackError("Paystack create subaccount timed out.") from exc
    except httpx.HTTPStatusError as exc:
        raise PaystackError(
            f"Paystack create subaccount failed: {exc.response.status_code} "
            f"{exc.response.text[:200]}"
        ) from exc

    if not data.get("status"):
        raise PaystackError(
            f"Paystack create subaccount returned status=false: {data.get('message')}"
        )

    sub = data["data"]
    return CreatedSubaccount(
        subaccount_code=sub["subaccount_code"],
        business_name=sub["business_name"],
    )
