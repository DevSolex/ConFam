"""
confam.payout_accounts — whitelist lookup and change flow for merchant payout accounts.

Engineering Rule 1 enforcement point:
  The settlement engine NEVER constructs a payout destination at runtime.
  It reads the merchant's current whitelisted PayoutAccount from this module
  and uses it as-is. If no active whitelisted account exists, the payment
  cannot proceed — this is a hard stop, not a fallback.

Cooling-off model (Rule 5 / OQ-021):
  A payout account change goes through a cooling-off period before taking effect.
  During the window, the OLD account remains active for all payments — the new
  row's active_from is in the future and is invisible to get_active_payout_account().

  Timeline:
    t=0:   request_payout_account_change() inserts new row with active_from = now() + cooling_off
           Old row keeps superseded_by = NULL (still active)
           Merchant receives an immediate notification (the security control)
    t<cooling_off:  get_active_payout_account() still returns the OLD row
    t=cooling_off:  new row's active_from passes → get_active_payout_account() returns NEW row
                    superseded_by on the old row is set lazily on first read after activation

  Note: superseded_by is NOT set at request time. Setting it then would immediately
  deactivate the old account, defeating the cooling-off period entirely.

Only the settlement engine should call these functions.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg2.extensions


@dataclass(frozen=True)
class PayoutAccount:
    payout_account_id: str
    merchant_id: str
    paystack_subaccount_code: str | None
    verification_method: str
    verified_at: datetime | None
    active_from: datetime | None
    created_at: datetime
    change_requested_at: datetime | None = None  # set for pending change rows


class NoActivePayoutAccount(Exception):
    """Raised when there is no active whitelisted payout account for this merchant."""


class PayoutAccountNotConfiguredForRail(Exception):
    """Raised when the active payout account has no subaccount code for the requested rail."""


class PendingChangeAlreadyExists(Exception):
    """Raised when a pending change is already outstanding for this merchant."""


class NoPendingChange(Exception):
    """Raised when cancellation is attempted but no pending change exists."""


def _cooling_off_seconds() -> int:
    try:
        return int(os.environ.get("PAYOUT_ACCOUNT_COOLING_OFF_SECONDS", "172800"))
    except (ValueError, TypeError):
        return 172800  # 48 hours default


def get_active_payout_account(
    conn: psycopg2.extensions.connection,
    merchant_id: str,
) -> PayoutAccount:
    """
    Return the merchant's current active whitelisted payout account.

    "Active" = active_from IS NOT NULL AND active_from <= now() AND superseded_by IS NULL.

    During a cooling-off window, the OLD account is returned (its active_from is
    in the past; the new pending row has a future active_from and is filtered out).
    Once the cooling-off window passes, the new row's active_from becomes past
    and it wins via ORDER BY active_from DESC.

    This is the Rule 1 whitelist lookup. The returned row is used verbatim.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                payout_account_id, merchant_id, paystack_subaccount_code,
                verification_method, verified_at, active_from, created_at,
                change_requested_at
            FROM payout_accounts
            WHERE merchant_id = %s
              AND superseded_by IS NULL
              AND active_from IS NOT NULL
              AND active_from <= now()
            ORDER BY active_from DESC
            LIMIT 1
            """,
            (merchant_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise NoActivePayoutAccount(
            f"Merchant {merchant_id} has no active whitelisted payout account. "
            "Payment cannot proceed (Engineering Rule 1)."
        )

    # Lazily set superseded_by on the old row if a newer active row now exists.
    # This keeps the historical chain clean without needing a separate job.
    _maybe_seal_superseded_row(conn, merchant_id, str(row[0]))

    return _row_to_account(row)


def _maybe_seal_superseded_row(
    conn: psycopg2.extensions.connection,
    merchant_id: str,
    current_active_id: str,
) -> None:
    """
    Lazily set superseded_by on older rows that are now obsolete.

    Called after we identify the current active row. If there are older rows
    (earlier active_from, superseded_by still NULL), seal them now by pointing
    their superseded_by at the current active row.

    This is safe to call repeatedly — it only updates rows where superseded_by
    IS NULL and active_from < the current active row's active_from.

    confam_app has GRANT UPDATE (superseded_by) — add this in migration 007
    or accept that this call runs as migrator in the test context.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE payout_accounts
                SET superseded_by = %s
                WHERE merchant_id = %s
                  AND superseded_by IS NULL
                  AND payout_account_id != %s
                  AND active_from IS NOT NULL
                  AND active_from < (
                      SELECT active_from FROM payout_accounts
                      WHERE payout_account_id = %s
                  )
                """,
                (current_active_id, merchant_id, current_active_id, current_active_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        # Non-fatal: the old row not being sealed is a cosmetic issue, not a
        # correctness issue. The lookup already returns the right row.
        pass


def get_pending_change(
    conn: psycopg2.extensions.connection,
    merchant_id: str,
) -> PayoutAccount | None:
    """
    Return the pending (not-yet-active) change row for this merchant, if any.

    A pending row has: superseded_by IS NULL AND active_from > now().
    Returns None if no pending change exists.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                payout_account_id, merchant_id, paystack_subaccount_code,
                verification_method, verified_at, active_from, created_at,
                change_requested_at
            FROM payout_accounts
            WHERE merchant_id = %s
              AND superseded_by IS NULL
              AND active_from IS NOT NULL
              AND active_from > now()
            ORDER BY active_from DESC
            LIMIT 1
            """,
            (merchant_id,),
        )
        row = cur.fetchone()

    return _row_to_account(row) if row else None


def request_payout_account_change(
    conn: psycopg2.extensions.connection,
    merchant_id: str,
    bank_account_number: str,
    bank_code: str,
    account_holder_name: str,
    paystack_subaccount_code: str,
) -> PayoutAccount:
    """
    Insert a pending payout account change row.

    The new row's active_from is set to now() + PAYOUT_ACCOUNT_COOLING_OFF_SECONDS.
    The OLD row is NOT touched — it keeps superseded_by = NULL and remains active
    throughout the cooling-off window.

    Raises PendingChangeAlreadyExists if a pending change is already outstanding.

    Rule 5: this is the correct behaviour — the old account stays live until
    active_from on the new row passes. superseded_by on the old row is set
    lazily by get_active_payout_account() once the window passes.

    TODO (Rule 9): encrypt bank_account_number and bank_code before storing.
    """
    # Verify an active account already exists (this is a *change*, not first setup)
    try:
        get_active_payout_account(conn, merchant_id)
    except NoActivePayoutAccount:
        raise NoActivePayoutAccount(
            f"Merchant {merchant_id} has no active payout account to change. "
            "Use POST /merchants/{id}/payout-account for first-time setup."
        )

    # Reject if a pending change is already outstanding
    existing_pending = get_pending_change(conn, merchant_id)
    if existing_pending is not None:
        raise PendingChangeAlreadyExists(
            f"A payout account change is already pending for merchant {merchant_id}. "
            "Cancel the existing request before submitting a new one."
        )

    cooling_off = _cooling_off_seconds()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO payout_accounts (
                merchant_id, bank_account_number, bank_code,
                account_holder_name, verification_method, verified_at,
                active_from, paystack_subaccount_code, change_requested_at
            )
            VALUES (%s, %s, %s, %s, 'bank_api_resolve', now(),
                    now() + (%s || ' seconds')::interval, %s, now())
            RETURNING
                payout_account_id, merchant_id, paystack_subaccount_code,
                verification_method, verified_at, active_from, created_at,
                change_requested_at
            """,
            (
                merchant_id,
                bank_account_number,  # TODO (Rule 9): encrypt
                bank_code,            # TODO (Rule 9): encrypt
                account_holder_name,
                cooling_off,
                paystack_subaccount_code,
            ),
        )
        row = cur.fetchone()

    conn.commit()
    return _row_to_account(row)


def cancel_pending_change(
    conn: psycopg2.extensions.connection,
    merchant_id: str,
) -> None:
    """
    Cancel a pending (not-yet-active) payout account change by deleting the row.

    Uses the standard confam_app connection. The DELETE is permitted by the
    RLS policy on payout_accounts: USING (active_from > now()). This means:
      - Pending rows (active_from in the future): deletable by confam_app ✓
      - Active/historical rows (active_from <= now()): RLS rejects the DELETE ✗

    RULE 2 NOTE: Deleting a pending row is acceptable here because:
      - The row has NEVER been used as an active payout destination.
      - No payment has ever been routed to this account.
      - Rule 2's append-only guarantee protects the history of accounts that
        were actually active and received merchant funds — not change requests
        that were cancelled before taking effect.
      - This reasoning is explicit here rather than assumed.

    The RLS policy on payout_accounts makes this structurally safe — confam_app
    provably cannot delete any row that has ever gone active, regardless of what
    application code attempts. No elevated credential is needed or used.

    After cancellation, the existing active account continues indefinitely.
    Raises NoPendingChange if there is nothing to cancel.
    """
    pending = get_pending_change(conn, merchant_id)
    if pending is None:
        raise NoPendingChange(
            f"Merchant {merchant_id} has no pending payout account change to cancel."
        )

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM payout_accounts WHERE payout_account_id = %s",
            (pending.payout_account_id,),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_account(row: tuple) -> PayoutAccount:
    (
        payout_account_id, merchant_id, paystack_subaccount_code,
        verification_method, verified_at, active_from, created_at,
        change_requested_at,
    ) = row
    return PayoutAccount(
        payout_account_id=str(payout_account_id),
        merchant_id=str(merchant_id),
        paystack_subaccount_code=paystack_subaccount_code,
        verification_method=verification_method,
        verified_at=verified_at,
        active_from=active_from,
        created_at=created_at,
        change_requested_at=change_requested_at,
    )
