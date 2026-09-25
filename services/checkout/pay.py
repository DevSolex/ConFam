"""
services/checkout/pay.py — "Pay by bank transfer" endpoint.

POST /{link_id}/pay/bank
  - Whitelist check (before any Paystack call — Rule 1)
  - Initialize Paystack transaction with the merchant's subaccount code
  - Return the authorization URL for the buyer to redirect to

This endpoint is in the checkout service because it is buyer-facing.
The actual settlement logic (webhook, ledger write) lives in the
settlement-engine service.
"""

import os

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator

from confam.db import get_conn
from confam.links import get_link
from confam.payout_accounts import (
    NoActivePayoutAccount,
    PayoutAccountNotConfiguredForRail,
    get_active_payout_account,
)
from confam.paystack import PaystackError, initialize_transaction

log = structlog.get_logger()

router = APIRouter()


class InitiatePaymentRequest(BaseModel):
    # Paystack requires an email address to initialize a transaction.
    # The buyer supplies this on the checkout page.
    email: str = Field(..., description="Buyer's email address (required by Paystack)")


class InitiatePaymentResponse(BaseModel):
    authorization_url: str
    reference: str


@router.post("/{link_id}/pay/bank", response_model=InitiatePaymentResponse)
def initiate_bank_payment(link_id: str, body: InitiatePaymentRequest) -> InitiatePaymentResponse:
    """
    Initiate a Paystack bank transfer payment for the given PaymentLink.

    Flow:
      1. Load and validate the PaymentLink (must be payable: created/opened, not expired).
      2. Whitelist check: load the merchant's active PayoutAccount (Rule 1).
         If no active account, fail clearly — no payment can be processed.
      3. Verify the payout account is configured for the bank rail
         (has a Paystack subaccount code).
      4. Initialize the Paystack transaction — pass the subaccount code as-is
         from the whitelisted account, never construct it from user input.
      5. Return the authorization_url for the buyer to redirect to.

    The subaccount code in step 4 is the read-only value from the whitelisted
    PayoutAccount row. No code path modifies or supplements it (Rule 1).
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
                "This merchant's payout account is not configured for bank transfer. "
                "Please contact the merchant."
            ),
        )

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
        )
    except PaystackError as exc:
        log.error(
            "initiate_payment_paystack_error",
            link_id=link_id,
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
    )

    return InitiatePaymentResponse(
        authorization_url=tx.authorization_url,
        reference=tx.reference,
    )
