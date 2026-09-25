"""
confam.ledger — LedgerEntry writes and the correction-entry path.

Engineering Rule 2: the ledger is append-only. This module enforces that
in two ways:
  1. write_ledger_entry() only ever INSERTs. There is no update_ledger_entry().
  2. write_correction_entry() INSERTs a new row with entry_type='correction'
     referencing the original — it never mutates the original row.

The Postgres trigger and confam_app privilege revocation enforce Rule 2 at the
database layer regardless of what this module does. This module's discipline is
the application-layer complement to those structural guarantees.

Engineering Rule 6: amount_minor_units is always int (kobo). Validated here
before any write.

Engineering Rule 4: every ledger row carries the full FK traceability chain:
  ledger_entry_id → rail_event_id → link_id → merchant_id → payout_account_id
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg2.extensions


@dataclass(frozen=True)
class LedgerEntry:
    ledger_entry_id: str
    link_id: str
    merchant_id: str
    payout_account_id: str
    rail_event_id: str
    amount_minor_units: int   # kobo — Rule 6
    currency: str
    rail: str
    conversion_rate: object   # Decimal or None
    conversion_rate_source: str | None
    confirmed_at: datetime
    entry_type: str           # 'sale' or 'correction'
    corrects_entry_id: str | None
    created_at: datetime


class LedgerWriteError(Exception):
    """Raised when a ledger entry cannot be written (duplicate, constraint, etc.)."""


def write_ledger_entry(
    conn: psycopg2.extensions.connection,
    *,
    link_id: str,
    merchant_id: str,
    payout_account_id: str,
    rail_event_id: str,
    amount_minor_units: int,
    currency: str,
    rail: str,
    confirmed_at: datetime,
    conversion_rate=None,
    conversion_rate_source: str | None = None,
) -> LedgerEntry:
    """
    Write a 'sale' LedgerEntry row.

    Rule 2: this function only INSERTs. No UPDATE path exists.
    Rule 6: amount_minor_units must be int. Raises TypeError if not.
    Rule 4: all FK fields are required — the full traceability chain is enforced.

    The unique index on (rail_event_id) WHERE entry_type='sale' means a second
    call with the same rail_event_id will raise a UniqueViolation — this is
    the second layer of idempotency (the first is the UNIQUE on rail_events).
    Do not catch that exception here; let it propagate so the caller knows a
    duplicate was attempted.
    """
    if not isinstance(amount_minor_units, int) or isinstance(amount_minor_units, bool):
        raise TypeError(
            f"amount_minor_units must be int (kobo), got {type(amount_minor_units).__name__}. "
            "Engineering Rule 6: no floats in monetary values."
        )
    if amount_minor_units <= 0:
        raise ValueError(f"amount_minor_units must be positive, got {amount_minor_units}")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger_entries (
                link_id, merchant_id, payout_account_id, rail_event_id,
                amount_minor_units, currency, rail,
                conversion_rate, conversion_rate_source,
                confirmed_at, entry_type
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'sale')
            RETURNING
                ledger_entry_id, link_id, merchant_id, payout_account_id,
                rail_event_id, amount_minor_units, currency, rail,
                conversion_rate, conversion_rate_source,
                confirmed_at, entry_type, corrects_entry_id, created_at
            """,
            (
                link_id, merchant_id, payout_account_id, rail_event_id,
                amount_minor_units, currency, rail,
                conversion_rate, conversion_rate_source,
                confirmed_at,
            ),
        )
        row = cur.fetchone()

    conn.commit()
    return _row_to_entry(row)


def write_correction_entry(
    conn: psycopg2.extensions.connection,
    *,
    original_entry_id: str,
    reason: str,
    corrected_amount_minor_units: int,
    currency: str = "NGN",
) -> LedgerEntry:
    """
    Write a correction LedgerEntry that references the original row.

    Rule 2: corrections are new rows, never mutations of the original.
    The original row is never touched by this function. The DB trigger would
    reject any attempt to UPDATE it anyway, but this function never tries.

    The correction row carries the same FK chain as the original (link_id,
    merchant_id, payout_account_id, rail_event_id) so Rule 4 traceability
    is maintained across both the original and the correction.

    corrected_amount_minor_units: the amount as it should have been. Must be int.
    reason: free-text explanation for the audit trail (stored in a separate
    description-like field — we store it as the description is not on
    ledger_entries; instead we derive it from context). For now, stored in
    conversion_rate_source as a simple audit note.

    This function is called by reconciliation processes (OQ-013, future work),
    not by any user-facing or webhook code path.
    """
    if not isinstance(corrected_amount_minor_units, int) or isinstance(corrected_amount_minor_units, bool):
        raise TypeError(
            f"corrected_amount_minor_units must be int (kobo), got {type(corrected_amount_minor_units).__name__}."
        )

    # Load the original entry to carry forward its FK chain.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT link_id, merchant_id, payout_account_id, rail_event_id,
                   currency, rail, confirmed_at
            FROM ledger_entries
            WHERE ledger_entry_id = %s AND entry_type = 'sale'
            """,
            (original_entry_id,),
        )
        original = cur.fetchone()

    if original is None:
        raise LedgerWriteError(
            f"Original ledger entry {original_entry_id} not found or is not a 'sale' entry. "
            "Corrections must reference a 'sale' row."
        )

    (link_id, merchant_id, payout_account_id, rail_event_id,
     currency_orig, rail, confirmed_at) = original

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ledger_entries (
                link_id, merchant_id, payout_account_id, rail_event_id,
                amount_minor_units, currency, rail,
                conversion_rate_source,
                confirmed_at, entry_type, corrects_entry_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), 'correction', %s)
            RETURNING
                ledger_entry_id, link_id, merchant_id, payout_account_id,
                rail_event_id, amount_minor_units, currency, rail,
                conversion_rate, conversion_rate_source,
                confirmed_at, entry_type, corrects_entry_id, created_at
            """,
            (
                link_id, merchant_id, payout_account_id, rail_event_id,
                corrected_amount_minor_units, currency or currency_orig, rail,
                f"correction:{reason}",
                original_entry_id,
            ),
        )
        row = cur.fetchone()

    conn.commit()
    return _row_to_entry(row)


def _row_to_entry(row: tuple) -> LedgerEntry:
    (
        ledger_entry_id, link_id, merchant_id, payout_account_id,
        rail_event_id, amount_minor_units, currency, rail,
        conversion_rate, conversion_rate_source,
        confirmed_at, entry_type, corrects_entry_id, created_at,
    ) = row
    return LedgerEntry(
        ledger_entry_id=str(ledger_entry_id),
        link_id=str(link_id),
        merchant_id=str(merchant_id),
        payout_account_id=str(payout_account_id),
        rail_event_id=str(rail_event_id),
        amount_minor_units=int(amount_minor_units),
        currency=currency,
        rail=rail,
        conversion_rate=conversion_rate,
        conversion_rate_source=conversion_rate_source,
        confirmed_at=(
            confirmed_at if confirmed_at.tzinfo
            else confirmed_at.replace(tzinfo=timezone.utc)
        ),
        entry_type=entry_type,
        corrects_entry_id=str(corrects_entry_id) if corrects_entry_id else None,
        created_at=(
            created_at if created_at.tzinfo
            else created_at.replace(tzinfo=timezone.utc)
        ),
    )
