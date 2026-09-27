"""
services/checkout — FastAPI application entry point.

Current scope (vertical slice 1): render a PaymentLink by link_id,
enforce expiry/state, transition created → opened.

Not yet implemented in this service:
  - SSE endpoint for real-time payment confirmation (requires a confirmed
    payment to stream — nothing to push yet in this slice)
  - Bank transfer / Lobstr payment initiation UI
  - Expired/paid link confirmation pages (beyond the basic state check)

See docs/ARCHITECTURE.md §3.2 and services/checkout/README.md.
"""

from contextlib import asynccontextmanager
from datetime import timezone
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from confam.db import close_pool, get_conn
from confam.links import open_link, get_link
from services.checkout.pay import router as pay_router

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    close_pool()


app = FastAPI(
    title="ConFam Checkout",
    description="Buyer-facing checkout page service.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(pay_router)

# Rate limiting — in-process sliding window (production: replace with Redis/WAF)
from services.checkout.middleware import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware)


# ---------------------------------------------------------------------------
# Response model (used by tests and JSON clients)
# ---------------------------------------------------------------------------

class CheckoutDetail(BaseModel):
    link_id: str
    description: str
    amount_minor_units: int
    currency: str
    status: str
    expires_at: str


# ---------------------------------------------------------------------------
# Health check — must be defined BEFORE wildcard /{link_id} routes
# so FastAPI doesn't swallow /health as a link_id.
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "checkout"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/{link_id}/json", response_model=CheckoutDetail)
def get_checkout_json(link_id: str) -> CheckoutDetail:
    """
    JSON endpoint for the checkout page data. Used by tests and future
    frontend clients. Mirrors the same logic as the HTML endpoint.
    """
    return _load_and_open(link_id)


@app.get("/{link_id}", response_class=HTMLResponse)
def get_checkout_page(link_id: str) -> HTMLResponse:
    """
    Buyer-facing checkout page. Loads the PaymentLink, enforces expiry and
    state, transitions created → opened, and renders transaction details.

    The HTML here is intentionally minimal — a full frontend is out of scope
    for this vertical slice. The structure is what matters: correct state
    enforcement and the created → opened transition.
    """
    try:
        detail = _load_and_open(link_id)
    except HTTPException as exc:
        return HTMLResponse(
            content=_render_invalid_page(exc.detail),
            status_code=exc.status_code,
        )

    return HTMLResponse(
        content=_render_checkout_page(detail),
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Shared logic
# ---------------------------------------------------------------------------

def _load_and_open(link_id: str) -> CheckoutDetail:
    """
    Load a PaymentLink, enforce expiry/state rules, and transition
    created → opened. Returns CheckoutDetail on success.
    Raises HTTPException on any invalid state.
    """
    with get_conn() as conn:
        # open_link is idempotent: created → opened if eligible; no-op otherwise.
        link = open_link(conn, link_id)

    if link is None:
        raise HTTPException(status_code=404, detail="Payment link not found")

    # Enforce: expired links must not render transaction details.
    if link.is_expired and link.status not in ("paid", "settling", "logged"):
        # Mark as expired if it hasn't been already (best-effort; the
        # settlement engine owns authoritative expiry marking).
        raise HTTPException(
            status_code=410,
            detail="This payment link has expired and is no longer valid.",
        )

    # Enforce: terminal statuses (logged, failed, expired) must not render
    # a live checkout — buyer should see a clear "no longer valid" state.
    if link.status in ("paid", "settling", "logged"):
        raise HTTPException(
            status_code=410,
            detail="This payment has already been completed.",
        )

    if link.status == "failed":
        raise HTTPException(
            status_code=410,
            detail="This payment link is no longer valid.",
        )

    log.info("checkout_opened", link_id=link.link_id, status=link.status)

    return CheckoutDetail(
        link_id=link.link_id,
        description=link.description,
        amount_minor_units=link.amount_minor_units,
        currency=link.currency,
        status=link.status,
        expires_at=link.expires_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Minimal HTML rendering
# ---------------------------------------------------------------------------

def _render_checkout_page(detail: CheckoutDetail) -> str:
    # Format amount: kobo → naira with 2 decimal places.
    naira = detail.amount_minor_units / 100
    amount_display = f"₦{naira:,.2f}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ConFam — Pay {amount_display}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 480px; margin: 48px auto; padding: 0 24px; }}
    .amount {{ font-size: 2rem; font-weight: 700; margin: 16px 0; }}
    .description {{ color: #555; margin-bottom: 24px; }}
    .status {{ font-size: 0.85rem; color: #888; }}
    .methods {{ border-top: 1px solid #eee; padding-top: 24px; margin-top: 24px; }}
  </style>
</head>
<body>
  <h1>ConFam Payment</h1>
  <div class="description">{_escape(detail.description)}</div>
  <div class="amount">{amount_display}</div>
  <div class="status">Link ID: {_escape(detail.link_id)}</div>
  <div class="methods">
    <p><strong>Choose a payment method:</strong></p>
    <p>🏦 Bank transfer — coming soon</p>
    <p>⭐ Lobstr / Stellar — coming soon</p>
    <p><em>Payment confirmation will appear here automatically once your transfer clears.</em></p>
  </div>
</body>
</html>"""


def _render_invalid_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ConFam — Link unavailable</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 480px; margin: 48px auto; padding: 0 24px; }}
  </style>
</head>
<body>
  <h1>Link unavailable</h1>
  <p>{_escape(message)}</p>
  <p>If you believe this is an error, contact the merchant who sent you this link.</p>
</body>
</html>"""


def _escape(text: str) -> str:
    """Minimal HTML escaping to prevent XSS in rendered output."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )

# ---------------------------------------------------------------------------
# Health check — defined above, not here.
# ---------------------------------------------------------------------------

