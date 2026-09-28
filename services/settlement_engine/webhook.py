"""
services/settlement_engine/webhook.py — Paystack webhook handler.

Handles charge.success events from Paystack. Implements:
  1. Signature verification (security boundary — Rule 1)
  2. RailEvent write (idempotency — Rule 3)
  3. Subaccount re-verification (defense in depth — Rule 1)
  4. LedgerEntry write (Rule 2 — append-only)
  5. PaymentLink terminal state transition
  6. Merchant notification stub (log only — real WhatsApp delivery is a later task)

The handler must return HTTP 200 quickly, even before processing completes.
Paystack retries any non-200 response, which could produce duplicate events —
the RailEvent UNIQUE constraint is the idempotency backstop for retries.
See docs/ARCHITECTURE.md §3.3 and confam/paystack.py for the settlement model.
"""

import json
from datetime import datetime, timezone

import psycopg2.errors
import structlog
from fastapi import APIRouter, HTTPException, Request, Response

from confam.db import get_conn
from confam.ledger import write_ledger_entry
from confam.payout_accounts import (
    NoActivePayoutAccount,
    PayoutAccountNotConfiguredForRail,
    get_active_payout_account,
)
from confam.paystack import WebhookSignatureInvalid, verify_webhook_signature

log = structlog.get_logger()

router = APIRouter(prefix="/webhooks")


@router.post("/paystack")
async def paystack_webhook(request: Request) -> Response:
    """
    Receive and process a Paystack webhook event.

    Security: HMAC-SHA512 signature is verified on the raw request body
    before any payload field is read. An invalid signature returns 200
    immediately (so Paystack stops retrying) but logs the rejection.

    Idempotency: the RailEvent UNIQUE (rail, rail_reference) constraint
    rejects a duplicate delivery at the database level. A UniqueViolation
    means this reference was already processed — return 200, log, do nothing.

    This endpoint must return 200 in all cases (success, duplicate, error)
    because a non-200 causes Paystack to retry, which could flood the queue.
    Real error alerting is via structlog + Sentry, not HTTP status codes.
    """
    # Read raw body FIRST — signature verification requires the exact bytes
    # as received, before any JSON parsing.
    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")

    # Return 200 immediately if the signature is missing/invalid.
    # Logging the rejection is sufficient — don't expose why to the caller.
    try:
        verify_webhook_signature(raw_body, signature)
    except WebhookSignatureInvalid:
        # Log at warn level — a bad signature could indicate probing/forgery attempts.
        # We return 200 (not 4xx) to avoid giving the caller information about why
        # the request was rejected, but we never stay silent about it.
        log.warning(
            "paystack_webhook_signature_invalid",
            source_ip=request.client.host if request.client else "unknown",
            content_length=len(raw_body),
            has_signature_header=bool(signature),
        )
        return Response(status_code=200)

    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        log.error("paystack_webhook_invalid_json")
        return Response(status_code=200)

    event_type = event.get("event")

    if event_type == "charge.success":
        _handle_charge_success(event["data"])
    else:
        # We only handle charge.success for now. Other events are logged and ignored.
        log.info("paystack_webhook_ignored", event_type=event_type)

    return Response(status_code=200)


def _handle_charge_success(data: dict) -> None:
    """
    Process a verified charge.success event.

    Sequence (all-or-nothing per transaction, backed by DB commits):
      1. Extract rail_reference (Paystack's transaction reference).
      2. Resolve link_id from metadata.
      3. Decide the event's disposition (applied / duplicate / applied_after_expiry).
      4. Write RailEvent — UNIQUE constraint rejects retries (Rule 3).
      5. Re-verify subaccount matches current whitelist (Rule 1, defense in depth).
      6. Write LedgerEntry (Rule 2).
      7. Transition PaymentLink to 'logged'.
      8. Notify the merchant.

    WHY THE DISPOSITION IS DECIDED BEFORE THE INSERT
      rail_events.disposition is written in the INSERT because confam_app has
      INSERT but no UPDATE on that column (migration 013 explains why adding one
      was a bad idea). Every disposition must therefore be known by the time we
      insert, which is fine — all three are decidable from the link row alone.
    """
    rail_reference = data.get("reference")
    if not rail_reference:
        log.error("paystack_charge_success_missing_reference", data=data)
        return

    link_id = (data.get("metadata") or {}).get("confam_link_id")
    if not link_id:
        log.error(
            "paystack_charge_success_missing_link_id",
            reference=rail_reference,
        )
        return

    # Amount from Paystack is already in kobo (minor units).
    # Cast to int explicitly — Rule 6: no floats in monetary values.
    amount_minor_units = int(data.get("amount", 0))
    currency = data.get("currency", "NGN")

    # Subaccount code reported by Paystack in the event — used for re-verification.
    reported_subaccount = (data.get("subaccount") or {}).get("subaccount_code")

    with get_conn() as conn:
        # Step 1: Load the payment link to get the merchant_id.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT merchant_id, status, expires_at <= now() AS is_expired
                FROM payment_links WHERE link_id = %s
                """,
                (link_id,),
            )
            row = cur.fetchone()

        if row is None:
            log.error(
                "paystack_charge_success_link_not_found",
                reference=rail_reference,
                link_id=link_id,
            )
            return

        merchant_id, link_status, link_expired = str(row[0]), row[1], bool(row[2])

        # Step 2: Decide the disposition, before we insert.

        # A link that is already logged means ConFam has been paid twice for one
        # item. A buyer can legitimately reach this by starting a card payment,
        # not completing it, and then paying by transfer. The money is real and
        # ours to hold, so it must be recorded and refunded by hand — NOT
        # silently dropped, and NOT recorded as a second sale (Rule 2/11).
        if link_status == "logged":
            _record_duplicate_payment(
                conn,
                link_id=link_id,
                merchant_id=merchant_id,
                rail_reference=rail_reference,
                amount_minor_units=amount_minor_units,
                currency=currency,
                data=data,
            )
            return

        # The link expired before the money arrived. The sale is still recorded:
        # refusing money that has already been collected would strand the buyer
        # and the merchant, and the ledger would then contradict reality. This is
        # a visibility flag, not an incident — the sale is correct either way.
        disposition = "applied_after_expiry" if link_expired else "applied"

        # Step 3: Write RailEvent — UNIQUE (rail, rail_reference) enforces Rule 3.
        # processed stays FALSE for the normal path so a crash before the ledger
        # write is still recoverable by reconciliation (OQ-013).
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rail_events
                        (link_id, rail, rail_reference, raw_payload, disposition)
                    VALUES (%s, 'bank', %s, %s, %s)
                    RETURNING rail_event_id
                    """,
                    (link_id, rail_reference, json.dumps(data), disposition),
                )
                rail_event_id = str(cur.fetchone()[0])
            conn.commit()
        except psycopg2.errors.UniqueViolation:
            # Same Paystack reference arriving twice — a webhook retry, not a
            # second payment. This is normal and expected; the UNIQUE index is
            # the idempotency backstop and it works.
            conn.rollback()
            log.info(
                "paystack_charge_success_duplicate_ignored",
                reference=rail_reference,
                link_id=link_id,
            )
            return

        # Step 3: Whitelist re-verification — Rule 1, defense in depth.
        # The subaccount was fixed at initialization, but we re-check here
        # in case the merchant's payout account changed during the cooling-off
        # window between initialization and confirmation.
        try:
            active_account = get_active_payout_account(conn, merchant_id)
        except NoActivePayoutAccount:
            log.error(
                "paystack_charge_success_no_active_payout_account",
                reference=rail_reference,
                link_id=link_id,
                merchant_id=merchant_id,
            )
            # Mark the link as failed — we received payment but cannot route it.
            # This triggers Rule 10's failure-policy behavior (alerting, manual intervention).
            _mark_link_failed(conn, link_id)
            return

        if (
            reported_subaccount
            and active_account.paystack_subaccount_code
            and reported_subaccount != active_account.paystack_subaccount_code
        ):
            log.error(
                "paystack_charge_success_subaccount_mismatch",
                reference=rail_reference,
                link_id=link_id,
                merchant_id=merchant_id,
                reported=reported_subaccount,
                whitelisted=active_account.paystack_subaccount_code,
            )
            _mark_link_failed(conn, link_id)
            return

        # Step 4: Write LedgerEntry (Rule 2 — append-only).
        # Note: Paystack splits settlement automatically; the merchant's bank
        # account is credited on their settlement schedule (default: next business
        # day). This LedgerEntry records "payment confirmed by Paystack," not
        # "merchant's bank account credited."
        try:
            entry = write_ledger_entry(
                conn,
                link_id=link_id,
                merchant_id=merchant_id,
                payout_account_id=active_account.payout_account_id,
                rail_event_id=rail_event_id,
                amount_minor_units=amount_minor_units,
                currency=currency,
                rail="bank",
                confirmed_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            log.error(
                "paystack_charge_success_ledger_write_failed",
                reference=rail_reference,
                link_id=link_id,
                error=str(exc),
            )
            # Mark the RailEvent as unprocessed so reconciliation can pick it up.
            # Do not mark the link as failed — the payment was received; the issue
            # is internal. This creates a state where rail_event.processed=False
            # and link.status is still not logged — the reconciliation job (OQ-013)
            # will detect and alert on this.
            conn.rollback()
            return

        # Step 5: Transition PaymentLink to terminal success state.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE payment_links SET status = 'logged' WHERE link_id = %s",
                (link_id,),
            )
            # Mark the rail event as processed.
            cur.execute(
                "UPDATE rail_events SET processed = TRUE WHERE rail_event_id = %s",
                (rail_event_id,),
            )
            # Fetch the merchant's confam_thread_id for the notification.
            cur.execute(
                "SELECT confam_thread_id FROM merchants WHERE merchant_id = %s",
                (merchant_id,),
            )
            confam_thread_id_row = cur.fetchone()
        conn.commit()

        # Step 6: Merchant notification — real WhatsApp message via messaging service.
        # If Twilio is unreachable the error is logged but settlement is NOT re-triggered:
        # the ledger entry is already written and the payment is already confirmed.
        # Rule 10: notification failure is recoverable; payout failure is not.
        if confam_thread_id_row:
            from services.messaging.main import send_payment_confirmed_notification
            send_payment_confirmed_notification(
                merchant_confam_thread_id=confam_thread_id_row[0],
                amount_minor_units=amount_minor_units,
                link_id=link_id,
            )
        else:
            log.warning(
                "merchant_notification_skipped_no_thread_id",
                merchant_id=merchant_id,
                link_id=link_id,
            )

        # The sale is correct either way, so this is a warning for visibility,
        # not an incident: no refund is due, and Sentry would cry wolf the first
        # time a slow bank transfer clears just after a link's 30-minute window.
        if disposition == "applied_after_expiry":
            log.warning(
                "payment_received_after_link_expiry",
                link_id=link_id,
                merchant_id=merchant_id,
                reference=rail_reference,
                rail_event_id=rail_event_id,
                amount_minor_units=amount_minor_units,
                note="Sale recorded as normal. Flagged so slow settlements on "
                "expired links are visible rather than surprising.",
            )

        log.info(
            "payment_confirmed_and_logged",
            link_id=link_id,
            merchant_id=merchant_id,
            rail_event_id=rail_event_id,
            ledger_entry_id=entry.ledger_entry_id,
            amount_minor_units=amount_minor_units,
            disposition=disposition,
        )


def _record_duplicate_payment(
    conn,
    *,
    link_id: str,
    merchant_id: str,
    rail_reference: str,
    amount_minor_units: int,
    currency: str,
    data: dict,
) -> None:
    """
    Record a second, distinct payment for a link that is already logged.

    The buyer UI lets one link be attempted more than once (card, then transfer),
    so a distinct reference arriving for a 'logged' link is a real, expected
    event — not an attack and not a webhook retry. It is also real money that
    ConFam is now holding twice over.

    What this does, and why each part matters:
      - Writes a RailEvent with disposition='duplicate'. Without a row there is
        no evidence the second payment exists, and the unique index on
        (rail, rail_reference) would then let a *retry* of this same event
        insert a second row later.
      - Sets processed=TRUE in the same INSERT. FALSE is the reconciliation
        job's "I have not dealt with this" signal; leaving a duplicate FALSE
        would make reconciliation retry it forever. TRUE here means "reviewed,
        and deliberately not applied" — not "a sale was written".
      - Writes NO LedgerEntry. One link, one sale, always (Rule 2 / Rule 11).
      - Raises a Sentry alert. This is money we owe back to a buyer, and it is
        not self-healing: someone has to issue the refund.
      - Still returns 200, because Paystack must not retry an event we have
        already accounted for.

    We deliberately do not refund automatically. A refund is an irreversible
    move on someone else's money and needs a human to confirm which of the two
    attempts the buyer actually intended to keep.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rail_events
                    (link_id, rail, rail_reference, raw_payload, processed, disposition)
                VALUES (%s, 'bank', %s, %s, TRUE, 'duplicate')
                RETURNING rail_event_id
                """,
                (link_id, rail_reference, json.dumps(data)),
            )
            rail_event_id = str(cur.fetchone()[0])
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        # Paystack retried the duplicate event itself. Already recorded.
        conn.rollback()
        log.info(
            "paystack_duplicate_payment_already_recorded",
            reference=rail_reference,
            link_id=link_id,
        )
        return

    log.error(
        "paystack_duplicate_payment",
        link_id=link_id,
        merchant_id=merchant_id,
        reference=rail_reference,
        rail_event_id=rail_event_id,
        amount_minor_units=amount_minor_units,
        currency=currency,
        note="DUPLICATE — link already logged, no sale written, REFUND MANUALLY",
    )

    # Rule 7: a mismatch we cannot resolve in code is an incident, not a retry.
    # Sent only after the DB commit above, so an alert never describes a row
    # that failed to persist.
    try:
        import sentry_sdk

        sentry_sdk.capture_message(
            f"Duplicate payment: link {link_id} was already paid, and a second "
            f"distinct charge of {amount_minor_units} {currency} arrived "
            f"(Paystack reference {rail_reference}, merchant {merchant_id}). "
            f"No second sale was written (rail_event {rail_event_id}, "
            f"disposition='duplicate'). A refund must be issued manually. "
            f"Reconcile the merchant's Paystack subaccount balance against the "
            f"ledger before refunding.",
            level="error",
        )
    except Exception as exc:  # pragma: no cover - alerting must never break settlement
        log.error("duplicate_payment_sentry_failed", error=str(exc))


def _mark_link_failed(conn, link_id: str) -> None:
    """Mark a PaymentLink as failed. Used when settlement cannot complete."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE payment_links SET status = 'failed' WHERE link_id = %s",
                (link_id,),
            )
        conn.commit()
        log.warning("payment_link_marked_failed", link_id=link_id)
    except Exception as exc:
        log.error("payment_link_mark_failed_error", link_id=link_id, error=str(exc))
        conn.rollback()
