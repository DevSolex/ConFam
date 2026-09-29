"""
services/checkout/pay.py — buyer payment-initiation endpoint.

POST /{link_id}/pay          (canonical; also reachable as /{link_id}/pay/bank)
  - Whitelist check (before any Paystack call — Rule 1)
  - Initialize Paystack transaction with the merchant's subaccount code,
    restricted to the channel the buyer chose
  - Return the authorization URL for the buyer to redirect to

This endpoint is in the checkout service because it is buyer-facing.
The actual settlement logic (webhook, ledger write) lives in the
settlement-engine service.

PCI SCOPE — read this before adding a field
    There are deliberately no card fields here. No number, no expiry, no CVV.
    Card details are entered only on Paystack's hosted checkout page, which is
    what keeps ConFam out of PCI scope. ConFam's page chooses a *method* and
    redirects; it never touches card data. Do not "improve" this by adding an
    inline card form.
"""

import os
import re
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from confam.db import get_conn
from confam.links import get_link
from confam.payout_accounts import (
    NoActivePayoutAccount,
    get_active_payout_account,
)
from confam.paystack import PaystackError, initialize_transaction

log = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Buyer-facing method -> Paystack channel
# ---------------------------------------------------------------------------
#
# These names are OURS, not Paystack's. The UI speaks "card"/"bank_transfer"/
# "bank"/"ussd"; this table is the only place that knows Paystack calls the
# same thing differently (or not at all).
#
#   card          -> ["card"]           Paystack hosted card form
#   bank_transfer -> ["bank_transfer"]  temporary account number, buyer transfers in
#   bank          -> ["bank"]           Pay-with-Bank; OPay is an option *inside*
#                                       this channel, not a channel of its own
#   ussd          -> ["ussd"]           dial a code; Nigerian customers only
#
# Deliberately NOT offered: "qr", "eft", "mobile_money". EFT is South Africa,
# mobile_money is Ghana, QR has no Nigerian consumer story worth the support
# load at pilot size. Stellar/Lobstr is out of scope (OQ-023/OQ-024).

METHOD_CHANNELS: dict[str, list[str]] = {
    "card": ["card"],
    "bank_transfer": ["bank_transfer"],
    "bank": ["bank"],
    "ussd": ["ussd"],
}

# Buyers get a friendly error, not our vocabulary. Kept separate from the
# validation error so the page can show something a person can act on.
METHOD_CHOICES = ", ".join(sorted(METHOD_CHANNELS))


# Pragmatic email check: one @, something either side, no spaces, a dot in the
# domain. Not RFC 5322 — the point is catching typos on a phone keyboard
# before spending a Paystack call, and Paystack does the real validation.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InitiatePaymentRequest(BaseModel):
    # Paystack requires an email address to initialize a transaction.
    # The buyer supplies this on the checkout page.
    email: str = Field(
        ...,
        max_length=254,
        description="Buyer's email address (required by Paystack, used for the receipt)",
    )

    # Which way the buyer wants to pay. Defaults to bank_transfer because that
    # is what this endpoint did before methods existed, so the original
    # /{link_id}/pay/bank callers keep working unchanged. Unknown values are
    # rejected with 422 rather than silently defaulting — a buyer who picked
    # "card" must never be charged via bank transfer because of a typo.
    method: str = Field(
        "bank_transfer",
        description=f"One of: {METHOD_CHOICES}",
    )

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        cleaned = value.strip()
        if not _EMAIL_RE.match(cleaned):
            raise ValueError(
                "Please enter a valid email address — Paystack sends your "
                "receipt there."
            )
        return cleaned

    @field_validator("method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in METHOD_CHANNELS:
            raise ValueError(
                f"Unsupported payment method '{value}'. "
                f"Choose one of: {METHOD_CHOICES}."
            )
        return cleaned


class InitiatePaymentResponse(BaseModel):
    authorization_url: str
    reference: str


def _callback_url(link_id: str, request: Request) -> str:
    """
    Where Paystack sends the buyer after they finish (or abandon) payment.

    Always our own checkout page for this link, never the Paystack dashboard,
    so the buyer can see whether the money actually landed.

    CHECKOUT_BASE_URL is authoritative when set: on Render it includes the
    "/pay" prefix that the checkout router is mounted under, which cannot be
    derived from the request. The request base URL is only a fallback for local
    development.

    This is server-side configuration, never buyer input — a callback_url taken
    from the request body would let a buyer bounce Paystack to a hostile page.
    """
    configured = (os.environ.get("CHECKOUT_BASE_URL") or "").strip()
    base = configured or str(request.base_url)
    base = base.rstrip("/")

    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        log.error(
            "checkout_base_url_invalid",
            scheme=parsed.scheme,
            has_netloc=bool(parsed.netloc),
        )
        raise HTTPException(
            status_code=500,
            detail="Payments are temporarily unavailable. Please try again shortly.",
        )

    return f"{base}/{link_id}"


@router.post("/{link_id}/pay", response_model=InitiatePaymentResponse)
@router.post("/{link_id}/pay/bank", response_model=InitiatePaymentResponse)
def initiate_payment(
    link_id: str, body: InitiatePaymentRequest, request: Request
) -> InitiatePaymentResponse:
    """
    Initialize a Paystack payment for the given PaymentLink, on the channel the
    buyer chose.

    Flow:
      1. Load and validate the PaymentLink (must be payable: created/opened, not expired).
      2. Whitelist check: load the merchant's active PayoutAccount (Rule 1).
         If no active account, fail clearly — no payment can be processed.
      3. Verify the payout account is configured for the rail
         (has a Paystack subaccount code).
      4. Initialize the Paystack transaction — pass the subaccount code as-is
         from the whitelisted account, never construct it from user input.
      5. Return the authorization_url for the buyer to redirect to.

    The subaccount code in step 4 is the read-only value from the whitelisted
    PayoutAccount row. No code path modifies or supplements it (Rule 1).

    The buyer is redirected to Paystack's hosted page and pays there. We never
    see card details — see the module docstring on PCI scope.
    """
    with get_conn() as conn:
        link = get_link(conn, link_id)

        if link is None:
            raise HTTPException(status_code=404, detail="Payment link not found")

        if link.is_expired:
            raise HTTPException(
                status_code=410,
                detail="This payment link has expired and can no longer be used.",
            )

        if link.status not in ("created", "opened"):
            raise HTTPException(
                status_code=410,
                detail="This payment link is no longer available for payment.",
            )

        # Whitelist check — Rule 1. Must happen before any Paystack call.
        try:
            payout_account = get_active_payout_account(conn, link.merchant_id)
        except NoActivePayoutAccount:
            log.error(
                "initiate_payment_no_payout_account",
                link_id=link_id,
                merchant_id=link.merchant_id,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "This merchant is not currently set up to receive payments. "
                    "Please contact the merchant."
                ),
            )

    # Check the payout account has a Paystack subaccount code.
    if not payout_account.paystack_subaccount_code:
        log.error(
            "initiate_payment_no_subaccount_code",
            link_id=link_id,
            merchant_id=link.merchant_id,
            payout_account_id=payout_account.payout_account_id,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "This merchant's payout account is not configured for this "
                "payment method. Please contact the merchant."
            ),
        )

    channels = METHOD_CHANNELS[body.method]
    callback_url = _callback_url(link.link_id, request)

    # Initialize the Paystack transaction.
    # The subaccount_code comes from the whitelisted PayoutAccount — never user input.
    try:
        tx = initialize_transaction(
            amount_minor_units=link.amount_minor_units,
            email=body.email,
            subaccount_code=payout_account.paystack_subaccount_code,
            reference=link.link_id,   # Use link_id as the Paystack reference
            link_id=link.link_id,
            transaction_charge_minor_units=0,  # TODO: add ConFam's platform fee here
            channels=channels,
            callback_url=callback_url,
        )
    except PaystackError as exc:
        log.error(
            "initiate_payment_paystack_error",
            link_id=link_id,
            method=body.method,
            error=str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail="Payment service temporarily unavailable. Please try again.",
        )

    log.info(
        "paystack_transaction_initialized",
        link_id=link_id,
        reference=tx.reference,
        merchant_id=link.merchant_id,
        method=body.method,
        channels=channels,
    )

    return InitiatePaymentResponse(
        authorization_url=tx.authorization_url,
        reference=tx.reference,
    )
