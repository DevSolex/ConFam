"""
app.py — Unified ASGI entrypoint for Render deployment.

Serves all three services (settlement-engine, checkout, messaging) on a
single port. Required because Render's free tier allows only one web service.

Route layout (designed to avoid collisions with checkout's /{link_id} wildcard):
  /health                        — health check (public)
  /webhooks/paystack             — Paystack webhook (public, HMAC-verified)
  /webhooks/whatsapp             — Meta webhook (public, HMAC-verified)
  /pay/{link_id}                 — checkout page (public, buyers)
  /pay/{link_id}/json            — checkout JSON (public)
  /pay/{link_id}/pay/bank        — initiate Paystack payment (public)
  /links                         — create payment link (admin-key required)
  /merchants                     — merchant creation (admin-key required)
  /merchants/{id}/payout-account — onboarding (admin-key required)
  /merchants/{id}/payout-account/change         — (admin-key required)
  /merchants/{id}/payout-account/change/cancel  — (admin-key required)
  /internal/reconcile            — reconciliation trigger (admin-key required)

Admin key: X-Admin-Key header must match ADMIN_API_KEY env var.
Compared in constant time. If ADMIN_API_KEY unset → 503 (fail closed).

CHECKOUT_BASE_URL must include the /pay prefix on Render:
  https://<service>.onrender.com/pay
"""

import hashlib
import hmac
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter as _APIRouter
from fastapi import FastAPI, Request
from fastapi import HTTPException as _HTTPException
from fastapi import Request as _Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from confam.db import close_pool, get_conn
from confam.links import LinkValidationError, create_link
from services.checkout.link_state import router as _status_router
from services.checkout.main import (
    CheckoutDetail,
    _load_and_open,
    _load_for_page,
    _render_checkout_page,
    _render_invalid_page,
)
from services.checkout.middleware import RateLimitMiddleware
from services.checkout.pay import router as _pay_router
from services.messaging.main import router as messaging_router
from services.settlement_engine.onboarding import router as onboarding_router
from services.settlement_engine.reconciliation import run_reconciliation
from services.settlement_engine.webhook import router as webhook_router

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Admin key middleware — protects non-public routes
# ---------------------------------------------------------------------------

# Routes that are public without an admin key
PUBLIC_PREFIXES = (
    "/health",
    "/webhooks/",
    "/pay/",
)


async def admin_key_middleware(request: Request, call_next):
    """
    Require X-Admin-Key for any route not in PUBLIC_PREFIXES.

    Compared with hmac.compare_digest (constant time) to prevent timing attacks.
    If ADMIN_API_KEY is not set, all protected routes return 503 (fail closed).
    """
    path = request.url.path

    is_public = any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)
    if is_public:
        return await call_next(request)

    admin_key = os.environ.get("ADMIN_API_KEY", "")
    if not admin_key:
        log.error("admin_api_key_not_set", path=path)
        return JSONResponse(
            status_code=503,
            content={"error": "service_unavailable", "detail": "Admin API not configured."},
        )

    provided = request.headers.get("X-Admin-Key", "")
    if not hmac.compare_digest(
        hashlib.sha256(provided.encode()).digest(),
        hashlib.sha256(admin_key.encode()).digest(),
    ):
        log.warning(
            "admin_key_rejected",
            path=path,
            source_ip=request.client.host if request.client else "unknown",
        )
        return JSONResponse(
            status_code=403,
            content={"error": "forbidden", "detail": "Invalid or missing X-Admin-Key header."},
        )

    return await call_next(request)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    close_pool()


app = FastAPI(
    title="ConFam",
    description="Unified ConFam API — settlement engine, checkout, and messaging.",
    version="1.0.0",
    lifespan=lifespan,
)

# Admin key middleware — runs before routing
app.middleware("http")(admin_key_middleware)

# Rate limiting on checkout (already in services/checkout/middleware.py)
app.add_middleware(RateLimitMiddleware)


# ---------------------------------------------------------------------------
# Health check (public)
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "confam"}


# ---------------------------------------------------------------------------
# Settlement engine routes
# ---------------------------------------------------------------------------

# Mount routers directly (avoid sub-application mounting which breaks middleware)
app.include_router(webhook_router)   # /webhooks/paystack
app.include_router(onboarding_router)  # /merchants/*


class CreateLinkRequest(BaseModel):
    merchant_id: str = Field(...)
    amount_minor_units: int = Field(..., gt=0)
    currency: str = Field(default="NGN")
    description: str = Field(..., min_length=1, max_length=500)

    @field_validator("amount_minor_units", mode="before")
    @classmethod
    def reject_floats(cls, v):
        if isinstance(v, float):
            raise ValueError("amount_minor_units must be an integer (kobo). Rule 6.")
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v):
        if v not in ("NGN",):
            raise ValueError(f"currency '{v}' is not supported")
        return v


class CreateLinkResponse(BaseModel):
    link_id: str
    checkout_url: str
    expires_at: str
    status: str


@app.post("/links", response_model=CreateLinkResponse, status_code=201)
def create_payment_link(body: CreateLinkRequest) -> CreateLinkResponse:
    checkout_base = os.environ.get("CHECKOUT_BASE_URL", "http://localhost:8001/pay")
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
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        log.error("create_link_failed", error=str(exc))
        from fastapi import HTTPException
        if "foreign key" in str(exc).lower():
            raise HTTPException(status_code=404, detail="merchant_id not found")
        raise HTTPException(status_code=500, detail="Internal error creating payment link")

    return CreateLinkResponse(
        link_id=link.link_id,
        checkout_url=f"{checkout_base.rstrip('/')}/{link.link_id}",
        expires_at=link.expires_at.isoformat(),
        status=link.status,
    )


@app.post("/internal/reconcile", status_code=200)
def trigger_reconciliation() -> dict:
    summary = run_reconciliation()
    return {"status": "complete", **summary}


@app.post("/internal/db-reset", status_code=200)
def db_reset() -> dict:
    """Temporary one-off: wipe all data and apply pending migrations. Remove after use."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                TRUNCATE ledger_entries, rail_events, payment_links,
                         payout_accounts, merchants
                RESTART IDENTITY CASCADE;
            """)
            cur.execute("""
                ALTER TABLE merchants
                ADD COLUMN IF NOT EXISTS country TEXT NOT NULL DEFAULT 'nigeria';
            """)
            try:
                cur.execute("""
                    ALTER TABLE merchants ADD CONSTRAINT merchants_country_check
                    CHECK (country IN ('nigeria', 'ghana'));
                """)
            except Exception:
                pass
            cur.execute("ALTER TABLE merchants ADD COLUMN IF NOT EXISTS pending_bank_code TEXT;")
            cur.execute("ALTER TABLE merchants ADD COLUMN IF NOT EXISTS pending_bank_selected_at TIMESTAMPTZ;")
            cur.execute("SELECT COUNT(*) FROM merchants;")
            merchant_count = cur.fetchone()[0]
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'merchants' ORDER BY ordinal_position;
            """)
            columns = [r[0] for r in cur.fetchall()]
        conn.commit()
    return {"status": "done", "merchants_remaining": merchant_count, "columns": columns}


# ---------------------------------------------------------------------------
# Checkout routes — mounted under /pay to avoid wildcard collision
# ---------------------------------------------------------------------------

checkout_router = _APIRouter(prefix="/pay")


@checkout_router.get("/{link_id}/json", response_model=CheckoutDetail)
def get_checkout_json(link_id: str) -> CheckoutDetail:
    return _load_and_open(link_id)


@checkout_router.get("/{link_id}", response_class=HTMLResponse)
def get_checkout_page(link_id: str, request: _Request) -> HTMLResponse:
    # Mirrors services.checkout.main.get_checkout_page. The handlers are
    # duplicated rather than imported because FastAPI needs a route object bound
    # to THIS router; the logic they call is shared, so the two cannot drift.
    try:
        detail, payable = _load_for_page(link_id)
    except _HTTPException as exc:
        return HTMLResponse(content=_render_invalid_page(exc.detail), status_code=exc.status_code)
    return HTMLResponse(
        content=_render_checkout_page(detail, request, payable=payable), status_code=200
    )


# Re-mount the pay and status routers under /pay.
#
# Do this by including the routers, not by copying route.path onto
# checkout_router: the copy-by-path loop that used to live here silently dropped
# each route's dependencies, tags and name, and would have dropped the new
# status route too.
checkout_router.include_router(_pay_router)
checkout_router.include_router(_status_router)

app.include_router(checkout_router)


# ---------------------------------------------------------------------------
# Messaging routes
# ---------------------------------------------------------------------------

app.include_router(messaging_router)  # /webhooks/whatsapp
