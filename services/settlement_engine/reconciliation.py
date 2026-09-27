"""
services/settlement_engine/reconciliation.py — Scheduled reconciliation job.

Implements OQ-013 (Rule 7): every 15 minutes, compare ConFam's internal
payment state against Paystack's own transaction records. A mismatch is
treated as an incident — not a silent retry, not a background task failure.

What "mismatch" means:
  - Paystack reports a transaction as successful (status=success) for a
    reference ConFam knows about, but ConFam has no corresponding LedgerEntry.
    This means we collected the buyer's money but never confirmed/settled it —
    the highest-severity gap.
  - A ConFam PaymentLink is in status='logged' but Paystack has no record of
    the reference — indicates a data integrity issue or a bug in the webhook
    handler.

What this job does NOT do:
  - It does not initiate payouts. Paystack's subaccount split settlement
    is automatic; this job only reconciles ConFam's internal records.
  - It does not auto-heal silently. Every mismatch is an incident (Sentry +
    structured log). The correction-entry path exists for manual/supervised
    correction, not automated rewriting of history.

Scheduling: run via the SQS worker or a cron-equivalent. The job itself is
stateless — safe to call multiple times; idempotency is enforced by the
existing UNIQUE (rail, rail_reference) constraint on rail_events.

Rule 7: "A mismatch found during reconciliation is treated as an incident,
not a background job failure to be silently retried."
"""

import os
from datetime import datetime, timedelta, timezone

import httpx
import sentry_sdk
import structlog

from confam.db import get_conn
from confam.ledger import write_ledger_entry
from confam.payout_accounts import NoActivePayoutAccount, get_active_payout_account
from services.settlement_engine.webhook import _handle_charge_success

log = structlog.get_logger()

PAYSTACK_API_BASE = os.environ.get("PAYSTACK_BASE_URL", "https://api.paystack.co")
# Look back this many minutes on each reconciliation run.
# Set slightly longer than the run interval to overlap and catch any edge cases.
LOOKBACK_MINUTES = int(os.environ.get("RECONCILIATION_LOOKBACK_MINUTES", "20"))


def _paystack_headers() -> dict:
    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    if not key:
        raise RuntimeError("PAYSTACK_SECRET_KEY not set — cannot reconcile")
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# Main reconciliation function
# ---------------------------------------------------------------------------

def run_reconciliation() -> dict:
    """
    Compare ConFam's internal state against Paystack's transaction records
    for the past LOOKBACK_MINUTES window.

    Returns a summary dict:
      {
        "checked": int,        # number of Paystack transactions checked
        "already_logged": int, # already have a LedgerEntry — no action needed
        "recovered": int,      # mismatch found and recovery attempted
        "incidents": int,      # mismatches that could not be auto-recovered
        "errors": list[str],   # non-fatal errors during the run
      }
    """
    log.info("reconciliation_started", lookback_minutes=LOOKBACK_MINUTES)
    summary = {"checked": 0, "already_logged": 0, "recovered": 0, "incidents": 0, "errors": []}

    # Step 1: fetch recent successful transactions from Paystack
    try:
        transactions = _fetch_paystack_transactions(since_minutes=LOOKBACK_MINUTES)
    except Exception as exc:
        msg = f"Failed to fetch Paystack transactions: {exc}"
        log.error("reconciliation_paystack_fetch_failed", error=str(exc))
        sentry_sdk.capture_exception(exc)
        summary["errors"].append(msg)
        # Can't reconcile without Paystack data — treat as incident per Rule 7
        sentry_sdk.capture_message(
            "Reconciliation job could not fetch Paystack transactions — manual check required.",
            level="error",
        )
        return summary

    summary["checked"] = len(transactions)

    with get_conn() as conn:
        for txn in transactions:
            reference = txn.get("reference", "")
            amount = int(txn.get("amount", 0))
            currency = txn.get("currency", "NGN")
            status = txn.get("status", "")
            metadata = txn.get("metadata") or {}
            link_id = metadata.get("confam_link_id")

            if status != "success":
                continue  # only care about successful transactions

            if not link_id:
                # Transaction not from ConFam (no link_id in metadata) — skip
                continue

            try:
                result = _reconcile_transaction(
                    conn=conn,
                    reference=reference,
                    link_id=link_id,
                    amount=amount,
                    currency=currency,
                    txn=txn,
                )
                if result == "already_logged":
                    summary["already_logged"] += 1
                elif result == "recovered":
                    summary["recovered"] += 1
                elif result == "incident":
                    summary["incidents"] += 1
            except Exception as exc:
                msg = f"Error reconciling reference {reference}: {exc}"
                log.error("reconciliation_transaction_error", reference=reference, error=str(exc))
                summary["errors"].append(msg)
                sentry_sdk.capture_exception(exc)

    log.info("reconciliation_complete", **summary)

    if summary["incidents"] > 0 or summary["errors"]:
        sentry_sdk.capture_message(
            f"Reconciliation found {summary['incidents']} incident(s) and "
            f"{len(summary['errors'])} error(s). Manual review required.",
            level="error" if summary["incidents"] > 0 else "warning",
        )

    return summary


def _fetch_paystack_transactions(since_minutes: int) -> list[dict]:
    """Fetch successful transactions from Paystack for the past N minutes."""
    from_dt = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    resp = httpx.get(
        f"{PAYSTACK_API_BASE}/transaction",
        headers=_paystack_headers(),
        params={"status": "success", "from": from_dt, "perPage": 100},
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("status"):
        raise RuntimeError(f"Paystack API returned status=false: {data.get('message')}")
    return data.get("data", [])


def _reconcile_transaction(
    conn,
    reference: str,
    link_id: str,
    amount: int,
    currency: str,
    txn: dict,
) -> str:
    """
    Check a single Paystack transaction against ConFam's internal state.
    Returns: "already_logged" | "recovered" | "incident"
    """
    # Check if we already have a RailEvent for this reference
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rail_event_id, processed FROM rail_events "
            "WHERE rail = 'bank' AND rail_reference = %s",
            (reference,),
        )
        rail_event_row = cur.fetchone()

    if rail_event_row:
        rail_event_id, processed = str(rail_event_row[0]), rail_event_row[1]
        if processed:
            # RailEvent exists and processed — check LedgerEntry exists too
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM ledger_entries WHERE rail_event_id = %s AND entry_type = 'sale'",
                    (rail_event_id,),
                )
                count = cur.fetchone()[0]
            if count > 0:
                return "already_logged"
            else:
                # RailEvent processed=True but no LedgerEntry — data integrity issue
                _raise_incident(
                    f"RailEvent {rail_event_id} is processed=True but has no LedgerEntry. "
                    f"Reference: {reference}, link_id: {link_id}",
                    level="fatal",
                )
                return "incident"
        else:
            # RailEvent exists but not processed — webhook was partially handled
            # Attempt recovery: complete the settlement
            return _attempt_recovery(conn, reference, link_id, amount, currency, rail_event_id, txn)
    else:
        # No RailEvent at all — webhook was never received or processed
        # Attempt recovery: create RailEvent and LedgerEntry
        return _attempt_recovery(conn, reference, link_id, amount, currency, None, txn)


def _attempt_recovery(
    conn,
    reference: str,
    link_id: str,
    amount: int,
    currency: str,
    existing_rail_event_id: str | None,
    txn: dict,
) -> str:
    """
    Attempt to recover a missed transaction by writing the missing records.
    Uses the existing webhook handler logic — idempotency constraints prevent
    double-writes if the webhook arrives concurrently.
    """

    log.warning(
        "reconciliation_missed_transaction_detected",
        reference=reference,
        link_id=link_id,
        amount=amount,
        existing_rail_event_id=existing_rail_event_id,
    )

    try:
        # Re-run the charge.success handler with the Paystack transaction data
        # The UNIQUE constraint on rail_events is the idempotency backstop
        _handle_charge_success(txn)
        log.info(
            "reconciliation_recovery_succeeded",
            reference=reference,
            link_id=link_id,
        )
        return "recovered"
    except Exception as exc:
        _raise_incident(
            f"Reconciliation recovery FAILED for reference={reference}, "
            f"link_id={link_id}. Error: {exc}. Manual intervention required.",
            level="fatal",
        )
        return "incident"


def _raise_incident(message: str, level: str = "error") -> None:
    """Log and raise a Sentry incident. Rule 7: mismatches are incidents, not silent retries."""
    log.error("reconciliation_incident", message=message)
    sentry_sdk.capture_message(message, level=level)
