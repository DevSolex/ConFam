"""
services/checkout/link_state.py — what the buyer is allowed to know.

The checkout page polls this after the buyer returns from Paystack, so it is
the only thing standing between a buyer-supplied URL and the internal state of
a payment link. It therefore exposes a deliberately coarse state, not the
internal one.

TWO RULES THIS MODULE ENFORCES
  1. A link's status is the ONLY proof of payment. Never the `reference` or
     `trxref` query parameters Paystack appends to the callback URL. Those are
     round-trippable by anyone holding the link, so treating them as proof would
     let a buyer show a "paid" page for a link they never paid. The webhook
     writes link.status; nothing the browser sends can.
  2. Terminal states are reported, not hidden. A buyer who paid needs to see
     "Payment received" even though the same link would 410 on a fresh GET of
     the checkout page. Polling an endpoint that rejected terminal links would
     be useless exactly when it matters.
"""

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from confam.db import get_conn
from confam.links import PaymentLink, get_link

log = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Buyer-facing state
# ---------------------------------------------------------------------------
#
# Coarser than link.status on purpose. A buyer does not care about the
# created/opened distinction (both just mean "not paid yet"); they care about
# one question — has my money arrived.
#
# Order matters when two conditions are both true:
#   logged  before expired — money arrived after the deadline, which happens
#            with bank transfers. The buyer paid; telling them it expired
#            would be both wrong and the fastest way to a chargeback.
#   failed  before expired — a rejected payment is more useful information
#            than "expired", and the page offers a retry either way.

BUYER_STATES = ("waiting", "logged", "failed", "expired")


def buyer_state(link: PaymentLink) -> str:
    """Map an internal PaymentLink to the state the buyer sees."""
    if link.status == "logged":
        return "logged"
    if link.status == "failed":
        return "failed"
    if link.is_expired:
        return "expired"
    return "waiting"


class LinkStatus(BaseModel):
    """Poll response. Contains nothing the buyer did not already have.

    Note the absence of merchant_id, payout account details, and any Paystack
    reference: none of it is the buyer's business, and link_id is public in the
    URL anyway.
    """

    state: str
    link_id: str
    description: str
    amount_minor_units: int
    currency: str
    merchant_name: str | None
    expires_at: str


def merchant_name_for(conn, merchant_id: str) -> str | None:
    """
    The merchant's buyer-facing name, or None if they have not set one.

    business_name is nullable (added in migration 009, after some merchants had
    already registered), so this genuinely can return None — the page falls back
    to a generic label rather than rendering "None".
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT business_name FROM merchants WHERE merchant_id = %s",
            (merchant_id,),
        )
        row = cur.fetchone()
    return (row[0].strip() if row and row[0] else None) or None


def load_for_buyer(conn, link_id: str) -> tuple[PaymentLink, str | None]:
    """
    Load a link plus its merchant's display name. No state enforcement — the
    caller decides what each state means for its response.

    Returns (link, merchant_name).
    """
    link = get_link(conn, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Payment link not found")
    return link, merchant_name_for(conn, link.merchant_id)


def to_status(link: PaymentLink, merchant_name: str | None) -> LinkStatus:
    return LinkStatus(
        state=buyer_state(link),
        link_id=link.link_id,
        description=link.description,
        amount_minor_units=link.amount_minor_units,
        currency=link.currency,
        merchant_name=merchant_name,
        expires_at=link.expires_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.get("/{link_id}/status", response_model=LinkStatus)
def get_link_status(link_id: str) -> LinkStatus:
    """
    Report the webhook-driven state of a payment link.

    The buyer page polls this after returning from Paystack. It is a read-only
    endpoint: it must never transition link state, or a buyer could "open" a
    link by polling it. It also does not call the created -> opened transition
    that the page GET performs.
    """
    with get_conn() as conn:
        link, merchant_name = load_for_buyer(conn, link_id)

    status = to_status(link, merchant_name)
    log.info(
        "checkout_status_polled",
        link_id=link.link_id,
        state=status.state,
    )
    return status
