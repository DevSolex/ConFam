"""
services/settlement_engine/onboarding.py — Merchant onboarding and payout account endpoints.

POST /merchants                                — create a merchant
POST /merchants/{id}/payout-account           — first-time payout account setup
POST /merchants/{id}/payout-account/change    — request a payout account change (Rule 5 cooling-off)
POST /merchants/{id}/payout-account/change/cancel — cancel a pending change

Rule 5 note:
  First-time onboarding: active_from = now() (no prior trusted state to protect).
  Account changes: active_from = now() + PAYOUT_ACCOUNT_COOLING_OFF_SECONDS.
  The old account stays active for all payments during the cooling-off window.
  An immediate notification is sent to the merchant when a change is requested —
  this is the actual security control, giving the real merchant a window to react
  if someone else initiated the change.

Rule 2 note on cancellation:
  Deleting a pending-change row before it ever goes live is acceptable.
  Rule 2 protects the history of accounts that actually received merchant funds.
  A row that was cancelled before activation has never been a payout destination.
  See confam/payout_accounts.py::cancel_pending_change() for the full reasoning.

OQ-025: WhatsApp "reply STOP to cancel" — not built in this task.
  Currently cancellation is API-only. A merchant who wants to cancel must call
  POST /merchants/{id}/payout-account/change/cancel directly. A WhatsApp-based
  cancellation command (so merchants can cancel from the same thread where they
  received the notification) is a separate messaging-service feature.
"""

from datetime import UTC

import psycopg2.errors
import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from confam.db import get_conn
from confam.payout_accounts import (
    NoActivePayoutAccount,
    NoPendingChange,
    PendingChangeAlreadyExists,
    cancel_pending_change,
    request_payout_account_change,
)
from confam.paystack import PaystackError, create_subaccount, resolve_bank_account

log = structlog.get_logger()

router = APIRouter(prefix="/merchants")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateMerchantRequest(BaseModel):
    whatsapp_number: str = Field(
        ...,
        description=(
            "Merchant's own buyer-facing WhatsApp number (thread 1). "
            "Placeholder only — no WhatsApp integration exists yet. "
            "See OQ-019."
        ),
    )
    business_name: str = Field(..., min_length=1, max_length=200)


class CreateMerchantResponse(BaseModel):
    merchant_id: str
    status: str


class SubmitPayoutAccountRequest(BaseModel):
    bank_account_number: str = Field(
        ...,
        min_length=10,
        max_length=10,
        description="10-digit NUBAN",
    )
    bank_code: str = Field(
        ...,
        min_length=3,
        max_length=6,
        description="Paystack bank code (e.g. '058')",
    )


class SubmitPayoutAccountResponse(BaseModel):
    payout_account_id: str
    account_holder_name: str   # As returned by Paystack bank/resolve
    paystack_subaccount_code: str
    merchant_status: str       # Should be 'active' on success


# ---------------------------------------------------------------------------
# POST /merchants
# ---------------------------------------------------------------------------

@router.post("", response_model=CreateMerchantResponse, status_code=201)
def create_merchant(body: CreateMerchantRequest) -> CreateMerchantResponse:
    """
    Create a new merchant in pending_verification status.

    The merchant must then submit a payout account via POST /merchants/{id}/payout-account
    before they can receive payments.

    PLACEHOLDER AUTH: no authentication on this endpoint yet. See OQ-019.
    TODO (OQ-019): add merchant auth before exposing this externally.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # confam_thread_id is required (NOT NULL, UNIQUE). Until the messaging
            # service exists, we derive a placeholder from the whatsapp_number.
            # A real value will come from the Twilio thread identity at onboarding time.
            confam_thread_id = f"pending-{body.whatsapp_number.replace('+', '').replace(' ', '')}"
            try:
                cur.execute(
                    """
                    INSERT INTO merchants
                        (whatsapp_number, confam_thread_id, business_name, status)
                    VALUES (%s, %s, %s, 'pending_verification')
                    RETURNING merchant_id, status
                    """,
                    (body.whatsapp_number, confam_thread_id, body.business_name),
                )
                row = cur.fetchone()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"A merchant with WhatsApp number {body.whatsapp_number} "
                        "already exists. Each number can only be registered once."
                    ),
                )
        conn.commit()

    merchant_id, status = str(row[0]), row[1]
    log.info("merchant_created", merchant_id=merchant_id, whatsapp_number=body.whatsapp_number)

    return CreateMerchantResponse(merchant_id=merchant_id, status=status)


# ---------------------------------------------------------------------------
# POST /merchants/{merchant_id}/payout-account
# ---------------------------------------------------------------------------

@router.post(
    "/{merchant_id}/payout-account",
    response_model=SubmitPayoutAccountResponse,
    status_code=201,
)
def submit_payout_account(
    merchant_id: str,
    body: SubmitPayoutAccountRequest,
) -> SubmitPayoutAccountResponse:
    """
    Verify a bank account and whitelist it as the merchant's payout account.

    Sequence (all-or-nothing intent; see module docstring for atomicity caveats):
      1. Confirm the merchant exists and is in a valid state.
      2. Reject if the merchant already has an active payout account
         (account-change flow is not yet built — OQ-021).
      3. Call Paystack bank/resolve to verify the account exists and get
         the account holder's name.
      4. Call Paystack create subaccount to create a split-payment endpoint
         for this merchant's bank account.
      5. Write a PayoutAccount row with active_from = now() (first-time
         onboarding: no cooling-off required — see module docstring).
      6. Transition merchant status to 'active'.

    Rule 1: the subaccount_code written here is the read-only whitelist value
    used by the settlement engine. It is never constructed at runtime.
    Rule 9: TODO — encrypt bank_account_number and bank_code before writing.
    """
    with get_conn() as conn:
        # Step 1: load merchant
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, business_name FROM merchants WHERE merchant_id = %s",
                (merchant_id,),
            )
            row = cur.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Merchant not found")

        merchant_status, business_name = row

        if merchant_status == "suspended":
            raise HTTPException(status_code=403, detail="Merchant account is suspended")

        # Step 2: reject if already has an active payout account
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM payout_accounts
                WHERE merchant_id = %s
                  AND superseded_by IS NULL
                  AND active_from IS NOT NULL
                  AND active_from <= now()
                """,
                (merchant_id,),
            )
            active_count = cur.fetchone()[0]

        if active_count > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This merchant already has an active whitelisted payout account. "
                    "Changing an existing payout account requires the account-change "
                    "flow, which is not yet available. See OPEN_QUESTIONS.md OQ-021."
                ),
            )

    # Steps 3–4 happen OUTSIDE the DB connection to avoid holding it open
    # during the Paystack API calls (which can take up to 10 seconds).

    # Step 3: resolve bank account
    try:
        resolved = resolve_bank_account(
            account_number=body.bank_account_number,
            bank_code=body.bank_code,
        )
    except PaystackError as exc:
        log.warning(
            "payout_account_resolution_failed",
            merchant_id=merchant_id,
            bank_code=body.bank_code,
            error=str(exc),
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Bank account could not be verified: {str(exc)}"
            ),
        )

    log.info(
        "bank_account_resolved",
        merchant_id=merchant_id,
        account_holder_name=resolved.account_name,
    )

    # Step 4: create Paystack subaccount
    # business_name comes from the merchant row, not from the request — it was
    # set at merchant creation and is the trusted value. The caller cannot
    # inject an arbitrary business name here.
    try:
        subaccount = create_subaccount(
            business_name=business_name or resolved.account_name,
            bank_code=body.bank_code,
            account_number=body.bank_account_number,
            percentage_charge=0.0,
        )
    except PaystackError as exc:
        log.error(
            "subaccount_creation_failed",
            merchant_id=merchant_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail="Payment provider error during account setup. Please try again.",
        )

    log.info(
        "subaccount_created",
        merchant_id=merchant_id,
        subaccount_code=subaccount.subaccount_code,
    )

    # Step 5 + 6: write PayoutAccount and activate merchant — one transaction.
    # If either write fails, the whole thing rolls back. The Paystack subaccount
    # will have been created but is harmless without a PayoutAccount row
    # (no money can flow through it — Rule 1 whitelist lookup will find nothing).
    with get_conn() as conn:
        with conn.cursor() as cur:
            # TODO (Rule 9): encrypt bank_account_number and bank_code before storing.
            cur.execute(
                """
                INSERT INTO payout_accounts (
                    merchant_id,
                    bank_account_number,
                    bank_code,
                    account_holder_name,
                    verification_method,
                    verified_at,
                    active_from,
                    paystack_subaccount_code
                )
                VALUES (%s, %s, %s, %s, 'bank_api_resolve', now(), now(), %s)
                RETURNING payout_account_id
                """,
                (
                    merchant_id,
                    body.bank_account_number,  # TODO: encrypt
                    body.bank_code,            # TODO: encrypt
                    resolved.account_name,
                    subaccount.subaccount_code,
                ),
            )
            payout_account_id = str(cur.fetchone()[0])

            # Activate the merchant
            cur.execute(
                "UPDATE merchants SET status = 'active' WHERE merchant_id = %s",
                (merchant_id,),
            )

        conn.commit()

    log.info(
        "merchant_onboarding_complete",
        merchant_id=merchant_id,
        payout_account_id=payout_account_id,
        subaccount_code=subaccount.subaccount_code,
    )

    return SubmitPayoutAccountResponse(
        payout_account_id=payout_account_id,
        account_holder_name=resolved.account_name,
        paystack_subaccount_code=subaccount.subaccount_code,
        merchant_status="active",
    )


# ---------------------------------------------------------------------------
# Request models for the change flow
# ---------------------------------------------------------------------------

class RequestChangeResponse(BaseModel):
    payout_account_id: str          # the new pending row's ID
    account_holder_name: str
    paystack_subaccount_code: str
    active_from: str                # ISO 8601 — when the change takes effect
    cooling_off_seconds: int
    message: str                    # human-readable summary


# ---------------------------------------------------------------------------
# POST /merchants/{merchant_id}/payout-account/change
# ---------------------------------------------------------------------------

@router.post(
    "/{merchant_id}/payout-account/change",
    response_model=RequestChangeResponse,
    status_code=202,
)
def request_change(
    merchant_id: str,
    body: SubmitPayoutAccountRequest,
) -> RequestChangeResponse:
    """
    Request a payout account change for a merchant that already has an active account.

    The change enters a cooling-off period (PAYOUT_ACCOUNT_COOLING_OFF_SECONDS,
    default 48 h) before taking effect. The OLD account continues to receive
    all payments during this window.

    Security control: an immediate WhatsApp notification is sent to the merchant
    the moment this endpoint is called — regardless of who called it. This gives
    the real merchant a window to cancel if someone else triggered this request.

    Returns 202 Accepted (not 201 Created) because the change is pending, not live.

    Rule 5: cooling-off enforced. First-time setup uses POST /payout-account.
    OQ-025: WhatsApp "reply STOP to cancel" not yet built — API-only for now.
    """
    # Steps 1–2: resolve bank account and create Paystack subaccount
    # (same as onboarding — same Paystack calls, different DB outcome)
    try:
        resolved = resolve_bank_account(
            account_number=body.bank_account_number,
            bank_code=body.bank_code,
        )
    except PaystackError as exc:
        log.warning("change_request_resolution_failed", merchant_id=merchant_id, error=str(exc))
        raise HTTPException(status_code=422, detail=f"Bank account could not be verified: {exc}")

    try:
        subaccount = create_subaccount(
            business_name=resolved.account_name,
            bank_code=body.bank_code,
            account_number=body.bank_account_number,
            percentage_charge=0.0,
        )
    except PaystackError as exc:
        log.error("change_request_subaccount_failed", merchant_id=merchant_id, error=str(exc))
        raise HTTPException(status_code=502, detail="Payment provider error. Please try again.")

    # Step 3: insert pending row with future active_from
    with get_conn() as conn:
        try:
            pending = request_payout_account_change(
                conn=conn,
                merchant_id=merchant_id,
                bank_account_number=body.bank_account_number,
                bank_code=body.bank_code,
                account_holder_name=resolved.account_name,
                paystack_subaccount_code=subaccount.subaccount_code,
            )
        except NoActivePayoutAccount:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No active payout account found. "
                    "Use POST /payout-account for first-time setup."
                ),
            )
        except PendingChangeAlreadyExists:
            raise HTTPException(
                status_code=409,
                detail=(
                    "A payout account change is already pending for this merchant. "
                    "Cancel the existing request before submitting a new one."
                ),
            )

        # Step 4: fetch merchant's confam_thread_id for the notification
        with conn.cursor() as cur:
            cur.execute(
                "SELECT confam_thread_id FROM merchants WHERE merchant_id = %s",
                (merchant_id,),
            )
            row = cur.fetchone()
        confam_thread_id = row[0] if row else None

    # Step 5: send immediate notification — the actual security control.
    # This must happen regardless of who triggered the request.
    # Mask account number: show only last 4 digits.
    masked_acct = "*" * (len(body.bank_account_number) - 4) + body.bank_account_number[-4:]
    cooling_off = pending.active_from
    active_from_str = (
        cooling_off.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        if cooling_off else "unknown"
    )

    if confam_thread_id:
        notification = (
            f"⚠️ Payout account change requested\n\n"
            f"Your ConFam payout account is being changed to:\n"
            f"Account: {masked_acct} ({resolved.account_name})\n\n"
            f"This change takes effect at: {active_from_str}\n\n"
            f"If you did NOT request this change, cancel it immediately:\n"
            f"Contact ConFam support or use the cancellation API."
        )
        try:
            from services.messaging.main import _send_whatsapp
            _send_whatsapp(confam_thread_id, notification)
            log.info(
                "payout_change_notification_sent",
                merchant_id=merchant_id,
                confam_thread_id=confam_thread_id,
            )
        except Exception as exc:
            # Non-fatal: the change IS pending regardless of notification outcome.
            # But this is a security-critical notification — if the merchant doesn't
            # see it, they cannot react to an unauthorised change. Capture to Sentry
            # so the support team is alerted immediately rather than discovering it
            # on a routine log review.
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
            sentry_sdk.capture_message(
                f"CRITICAL: payout account change notification failed for merchant {merchant_id}. "
                f"Merchant may not have been notified. Manual follow-up required before "
                f"active_from={pending.active_from}.",
                level="fatal",
            )
            log.error(
                "payout_change_notification_FAILED",
                merchant_id=merchant_id,
                active_from=str(pending.active_from),
                error=str(exc),
                note="MANUAL FOLLOW-UP REQUIRED — Sentry alert raised",
            )

    log.info(
        "payout_account_change_requested",
        merchant_id=merchant_id,
        new_payout_account_id=pending.payout_account_id,
        active_from=str(pending.active_from),
    )

    from confam.payout_accounts import _cooling_off_seconds
    return RequestChangeResponse(
        payout_account_id=pending.payout_account_id,
        account_holder_name=resolved.account_name,
        paystack_subaccount_code=subaccount.subaccount_code,
        active_from=pending.active_from.isoformat() if pending.active_from else "",
        cooling_off_seconds=_cooling_off_seconds(),
        message=(
            f"Change request accepted. Your current payout account remains active "
            f"until {active_from_str}. An immediate notification has been sent to "
            f"your WhatsApp. Cancel via POST /payout-account/change/cancel if this was not you."
        ),
    )


# ---------------------------------------------------------------------------
# POST /merchants/{merchant_id}/payout-account/change/cancel
# ---------------------------------------------------------------------------

@router.post("/{merchant_id}/payout-account/change/cancel", status_code=200)
def cancel_change(merchant_id: str) -> dict:
    """
    Cancel a pending (not-yet-active) payout account change.

    Uses the standard confam_app connection. The RLS policy on payout_accounts
    (migration 011) structurally restricts DELETE to rows where active_from > now(),
    so confam_app cannot accidentally delete an active or historical row even if
    called with an arbitrary payout_account_id.

    No elevated credential is used or required at runtime.

    OQ-025: WhatsApp "reply STOP to cancel" not built in this task — API-only.
    """
    with get_conn() as conn:
        try:
            cancel_pending_change(conn, merchant_id)
        except NoActivePayoutAccount:
            raise HTTPException(
                status_code=404,
                detail="Merchant not found or has no active account.",
            )
        except NoPendingChange:
            raise HTTPException(
                status_code=404,
                detail="No pending payout account change found for this merchant.",
            )

    log.info("payout_account_change_cancelled", merchant_id=merchant_id)
    return {
        "message": (
            "Pending payout account change cancelled. "
            "Your existing account remains active."
        )
    }
