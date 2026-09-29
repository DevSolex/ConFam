"""
confam.links — PaymentLink domain logic.

This module owns the lifecycle of a PaymentLink:
  - creation (validate inputs, persist, return URL)
  - retrieval (load by link_id, enforce expiry + state)
  - the created → opened state transition

It does NOT touch payment rails, settlement, ledger entries, or notifications.
Those belong in services/settlement-engine/ and will be added in a later task.

Engineering rules enforced here:
  Rule 6 — amount_minor_units is always int (kobo). Validated before DB write.
  Rule 1 — no payout logic in this module; payout destination is never touched.
"""

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg2.extensions

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES = ("created", "opened", "paid", "settling", "logged", "expired", "failed")
TERMINAL_STATUSES = ("paid", "settling", "logged", "expired", "failed")
VALID_CURRENCIES = ("NGN",)

# Expiry window in seconds. Reads from environment; default 1800 (30 min).
# OQ-012 resolved: 30 minutes from creation, or first successful payment.
def _expiry_seconds() -> int:
    try:
        return int(os.environ.get("PAYMENT_LINK_EXPIRY_SECONDS", "1800"))
    except (ValueError, TypeError):
        return 1800


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PaymentLink:
    link_id: str
    merchant_id: str
    amount_minor_units: int   # kobo — Rule 6: always int, never float
    currency: str
    description: str
    status: str
    expires_at: datetime
    created_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def can_be_opened(self) -> bool:
        """True only if the link is in a state that allows a buyer to view it."""
        return self.status in ("created", "opened") and not self.is_expired


# ---------------------------------------------------------------------------
# Input validation — application-layer, before any DB write
# ---------------------------------------------------------------------------

class LinkValidationError(ValueError):
    """Raised when create_link input fails validation."""


def _validate_create_inputs(
    merchant_id: str,
    amount_minor_units: int,
    currency: str,
    description: str,
) -> None:
    """
    Validate inputs for payment link creation.
    Raises LinkValidationError with a descriptive message on failure.

    Validation happens here, in the application layer, before any database
    interaction. We do not rely on DB constraints as the primary validator —
    they are a last-resort backstop, not the user-facing error path.
    """
    if not merchant_id or not merchant_id.strip():
        raise LinkValidationError("merchant_id is required")

    # Validate merchant_id is a valid UUID — avoids SQL injection risk and
    # gives a clear error before a DB round-trip that would fail anyway.
    try:
        uuid.UUID(merchant_id)
    except ValueError:
        raise LinkValidationError("merchant_id must be a valid UUID")

    # Rule 6: amount must be a positive integer (kobo). Reject floats explicitly.
    if not isinstance(amount_minor_units, int) or isinstance(amount_minor_units, bool):
        raise LinkValidationError(
            "amount_minor_units must be an integer (kobo). "
            "Floats are not permitted — Engineering Rule 6."
        )
    if amount_minor_units <= 0:
        raise LinkValidationError(
            f"amount_minor_units must be positive, got {amount_minor_units}"
        )

    if currency not in VALID_CURRENCIES:
        raise LinkValidationError(
            f"currency '{currency}' is not supported. Supported: {VALID_CURRENCIES}"
        )

    if not description or not description.strip():
        raise LinkValidationError("description is required")

    if len(description.strip()) > 500:
        raise LinkValidationError("description must be 500 characters or fewer")


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def create_link(
    conn: psycopg2.extensions.connection,
    merchant_id: str,
    amount_minor_units: int,
    currency: str,
    description: str,
) -> PaymentLink:
    """
    Validate inputs, persist a new PaymentLink row, and return the domain object.

    Uses the confam_app role connection — confam_app has INSERT on payment_links.
    Raises LinkValidationError on invalid inputs (before DB write).
    Raises psycopg2.errors.ForeignKeyViolation if merchant_id does not exist.
    """
    _validate_create_inputs(merchant_id, amount_minor_units, currency, description.strip())

    expires_at = datetime.now(UTC) + timedelta(seconds=_expiry_seconds())

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO payment_links
                (merchant_id, amount_minor_units, currency, description,
                 status, expires_at)
            VALUES (%s, %s, %s, %s, 'created', %s)
            RETURNING link_id, merchant_id, amount_minor_units, currency,
                      description, status, expires_at, created_at
            """,
            (merchant_id, amount_minor_units, currency, description.strip(), expires_at),
        )
        row = cur.fetchone()

    conn.commit()
    return _row_to_link(row)

def get_link(
    conn: psycopg2.extensions.connection,
    link_id: str,
) -> PaymentLink | None:
    """
    Load a PaymentLink by link_id. Returns None if not found.
    Does NOT transition state — use open_link() for that.
    """
    try:
        uuid.UUID(link_id)
    except ValueError:
        return None  # Not a valid UUID — treat as not found, not an error.

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT link_id, merchant_id, amount_minor_units, currency,
                   description, status, expires_at, created_at
            FROM payment_links
            WHERE link_id = %s
            """,
            (link_id,),
        )
        row = cur.fetchone()

    return _row_to_link(row) if row else None


def open_link(
    conn: psycopg2.extensions.connection,
    link_id: str,
) -> PaymentLink | None:
    """
    Transition a PaymentLink from 'created' to 'opened' (buyer opened the checkout page).

    Idempotent: if status is already 'opened', returns the link unchanged.
    Returns None if the link does not exist.
    Returns the current link unchanged (without error) if it is expired or terminal —
    the caller checks can_be_opened to decide what to render.

    confam_app holds GRANT UPDATE (status) ON payment_links — this is the only
    column being written. No other field is modified.
    """
    try:
        uuid.UUID(link_id)
    except ValueError:
        return None

    with conn.cursor() as cur:
        # Conditional UPDATE: only transitions created → opened.
        # Already-opened links are left untouched (idempotent).
        # Expired/terminal links are also left untouched — status stays as-is.
        cur.execute(
            """
            UPDATE payment_links
            SET status = 'opened'
            WHERE link_id = %s
              AND status = 'created'
              AND expires_at > now()
            """,
            (link_id,),
        )
        conn.commit()

    # Re-fetch to return current state (whether or not the UPDATE fired).
    return get_link(conn, link_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_link(row: tuple) -> PaymentLink:
    (
        link_id,
        merchant_id,
        amount_minor_units,
        currency,
        description,
        status,
        expires_at,
        created_at,
    ) = row
    return PaymentLink(
        link_id=str(link_id),
        merchant_id=str(merchant_id),
        amount_minor_units=int(amount_minor_units),  # Rule 6: explicit int cast
        currency=currency,
        description=description,
        status=status,
        expires_at=expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC),
        created_at=created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC),
    )
