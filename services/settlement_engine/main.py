"""
services/settlement-engine — FastAPI application entry point.

Current scope (vertical slice 1): payment link creation only.
  POST /links — create a PaymentLink and return its checkout URL.

Not yet implemented in this service:
  - Webhook handlers (Paystack, Stellar)
  - Settlement / whitelist check / payout logic
  - LedgerEntry writes
  - Merchant notification
  - Real merchant auth (placeholder merchant_id in request for now)

See docs/ARCHITECTURE.md §3.3 and services/settlement-engine/README.md.
"""

import os

import structlog
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from confam.db import close_pool, get_conn
from confam.links import LinkValidationError, create_link
from services.settlement_engine.webhook import router as webhook_router
from services.settlement_engine.onboarding import router as onboarding_router
from services.settlement_engine.reconciliation import run_reconciliation

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    close_pool()


app = FastAPI(
    title="ConFam Settlement Engine",
    description="Internal API for payment link creation and settlement.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(webhook_router)
app.include_router(onboarding_router)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateLinkRequest(BaseModel):
    # PLACEHOLDER AUTH: merchant_id is passed directly in the request body.
    # Real merchant authentication (session token, Twilio thread identity) is
    # a separate later task. This field is clearly a placeholder — do not ship
    # this endpoint to production without replacing it with proper auth.
    # See OPEN_QUESTIONS.md (new item OQ-019 if surfaced by this task).
    merchant_id: str = Field(..., description="Merchant UUID (placeholder auth — see task notes)")

    # Rule 6: amount_minor_units must be an integer (kobo). Pydantic will
    # coerce JSON numbers, but we validate explicitly that no float slips through.
    amount_minor_units: int = Field(..., gt=0, description="Amount in kobo (NGN smallest unit)")
    currency: str = Field(default="NGN")
    description: str = Field(..., min_length=1, max_length=500)

    @field_validator("amount_minor_units", mode="before")
    @classmethod
    def reject_floats(cls, v: object) -> int:
        # JSON has no integer/float distinction at the wire level, so 100.0
        # would deserialise as float. Reject any non-integer explicitly.
        # Rule 6: monetary values are exact integers only.
        if isinstance(v, float):
            raise ValueError(
                "amount_minor_units must be an integer (kobo). "
                "Floats are not permitted — Engineering Rule 6."
            )
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        if v not in ("NGN",):
            raise ValueError(f"currency '{v}' is not supported")
        return v


class CreateLinkResponse(BaseModel):
    link_id: str
    checkout_url: str
    expires_at: str  # ISO 8601
    status: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/links", response_model=CreateLinkResponse, status_code=201)
def create_payment_link(body: CreateLinkRequest) -> CreateLinkResponse:
    """
    Create a one-time payment link for a merchant.

    The returned checkout_url is what the merchant pastes into the buyer thread.
    See docs/ARCHITECTURE.md §2 (end-to-end flow).

    PLACEHOLDER AUTH: merchant_id is taken from the request body directly.
    This will be replaced with proper merchant auth in a later task.
    """
    checkout_base = os.environ.get("CHECKOUT_BASE_URL", "http://localhost:8001")

    try:
        with get_conn() as conn:
            link = create_link(
                conn=conn,
                merchant_id=body.merchant_id,
                amount_minor_units=body.amount_minor_units,
                currency=body.currency,
                description=body.description,
            )
    except LinkValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        # Log the real error; don't leak internals to the caller.
        log.error("create_link_failed", error=str(exc))
        # Surface FK violations as 404 (merchant not found) rather than 500.
        if "foreign key" in str(exc).lower():
            raise HTTPException(status_code=404, detail="merchant_id not found")
        raise HTTPException(status_code=500, detail="Internal error creating payment link")

    log.info("payment_link_created", link_id=link.link_id, merchant_id=link.merchant_id)

    return CreateLinkResponse(
        link_id=link.link_id,
        checkout_url=f"{checkout_base.rstrip('/')}/{link.link_id}",
        expires_at=link.expires_at.isoformat(),
        status=link.status,
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "settlement-engine"}


@app.post("/internal/reconcile", status_code=200)
def trigger_reconciliation() -> dict:
    """
    Trigger a reconciliation run against Paystack's transaction records.

    This endpoint is called by the SQS worker on a 15-minute schedule (OQ-013).
    It is on an /internal/ path — in production, restrict this to the Docker
    network only (same isolation as the rest of settlement-engine, OQ-022).

    Rule 7: mismatches are incidents, not silent retries. The job raises
    Sentry alerts for any gap between Paystack's records and our ledger.
    """
    summary = run_reconciliation()
    return {"status": "complete", **summary}
