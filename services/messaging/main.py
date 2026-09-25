"""
services/messaging/main.py — Meta WhatsApp Cloud API messaging service.

Replaces the Twilio BSP transport with the Meta Cloud API directly.
Decision rationale: Meta Cloud API avoids long-term per-message BSP costs
and was a deliberate switch after hands-on testing — not a reversal of the
original BSP decision. See OQ-001 in OPEN_QUESTIONS.md.

This service owns:
  - Webhook verification handshake (GET /webhooks/whatsapp — Meta one-time check).
  - Signature verification on inbound webhooks (X-Hub-Signature-256).
  - Resolving the sender's WhatsApp ID to a merchant (via confam_thread_id).
  - Parsing the PAY command and creating payment links.
  - Replying to the merchant with the checkout URL.
  - Sending payment-confirmed notifications to merchants.

What this service does NOT do:
  - Message buyers directly (Rule: ConFam never messages thread 1).
  - Handle any payment, settlement, or ledger logic.

confam_thread_id format for Meta Cloud API:
  Meta sends bare digits without prefix — e.g. "2348012345678" (no '+', no 'whatsapp:').
  This differs from the Twilio format ("whatsapp:+2348012345678").
  The confam_thread_id stored in the merchants table must use the bare-digit format
  when using Meta Cloud API. See docs/DATA_MODEL.md §1 (Merchant).

Command format (one explicit command, no NLP):
  PAY <amount in naira> <description>
  Example: PAY 750 Ankara fabric x2
"""

import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
import structlog
from fastapi import FastAPI, Request, Response
from fastapi import APIRouter

from confam.db import close_pool, get_conn
from confam.links import LinkValidationError, create_link

log = structlog.get_logger()

META_API_VERSION = "v20.0"
META_API_BASE = "https://graph.facebook.com"


# ---------------------------------------------------------------------------
# Meta Cloud API signature verification
# ---------------------------------------------------------------------------

def _verify_meta_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Verify the X-Hub-Signature-256 header.

    Meta signs the raw request body with HMAC-SHA256 using your App Secret.
    Format: "sha256=<hex_digest>"

    An invalid or missing signature means the request did not come from Meta.
    This is the security boundary of the inbound webhook endpoint — without it,
    anyone could fabricate a merchant's WhatsApp message.

    Returns True if valid, False if not.
    """
    app_secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not app_secret:
        log.error("whatsapp_app_secret_not_set")
        return False

    if not signature_header.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Meta Cloud API outbound send
# ---------------------------------------------------------------------------

def _send_whatsapp(to: str, body: str) -> None:
    """
    Send a WhatsApp text message to `to` via Meta Cloud API.

    `to` is the recipient's bare WhatsApp ID (digits only, no '+' or 'whatsapp:').
    This matches the confam_thread_id format for Meta Cloud API.

    Rule 10: if the API is unreachable, log the error and continue.
    The caller must never crash because a notification failed.

    Engineering Rule 8: WHATSAPP_ACCESS_TOKEN is read from environment only.
    """
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")

    if not phone_number_id or not access_token:
        log.error("whatsapp_credentials_not_set")
        return

    try:
        response = httpx.post(
            f"{META_API_BASE}/{META_API_VERSION}/{phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            },
            timeout=10.0,
        )
        response.raise_for_status()
        log.info("whatsapp_message_sent", to=to, body_length=len(body))
    except Exception as exc:
        log.error("whatsapp_send_failed", to=to, error=str(exc))


# ---------------------------------------------------------------------------
# Message parsing (unchanged from Twilio version)
# ---------------------------------------------------------------------------

class ParseError(ValueError):
    """Raised when the merchant's message doesn't match the PAY command format."""


USAGE_MESSAGE = (
    "Sorry, I didn't understand that.\n\n"
    "To create a payment link, send:\n"
    "  PAY <amount in naira> <description>\n\n"
    "Example:\n"
    "  PAY 750 Ankara fabric x2"
)


def parse_pay_command(text: str) -> tuple[int, str]:
    """
    Parse 'PAY <amount> <description>' from the merchant's message.

    Returns (amount_minor_units, description).
    Raises ParseError with a user-friendly message on any parse failure.

    Amount is supplied in naira and converted to kobo (×100) here.
    Rule 6: the resulting amount_minor_units is always int.
    """
    parts = text.strip().split(None, 2)
    if len(parts) < 3 or parts[0].upper() != "PAY":
        raise ParseError(USAGE_MESSAGE)

    naira_str = parts[1]
    description = parts[2].strip()

    if not description:
        raise ParseError(USAGE_MESSAGE)

    try:
        naira = float(naira_str)
    except ValueError:
        raise ParseError(f"'{naira_str}' is not a valid amount.\n\n{USAGE_MESSAGE}")

    if naira <= 0:
        raise ParseError(f"Amount must be greater than zero.\n\n{USAGE_MESSAGE}")

    kobo = naira * 100
    if kobo != int(kobo):
        raise ParseError(
            f"Amount must be in whole kobo (e.g. '750' or '750.00').\n\n{USAGE_MESSAGE}"
        )

    return int(kobo), description


# ---------------------------------------------------------------------------
# Merchant resolution (updated for Meta's bare-digit ID format)
# ---------------------------------------------------------------------------

class UnregisteredSender(Exception):
    """Raised when the sender's WhatsApp ID isn't registered as a merchant."""


def resolve_merchant(conn, from_id: str) -> dict:
    """
    Look up the merchant whose confam_thread_id matches `from_id`.

    For Meta Cloud API, `from_id` is a bare digits string (e.g. "2348012345678").
    This is stored directly as confam_thread_id — no 'whatsapp:+' prefix.

    See docs/DATA_MODEL.md §1 (Merchant) for the confam_thread_id format note.

    Raises UnregisteredSender if no match.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT merchant_id, status FROM merchants WHERE confam_thread_id = %s",
            (from_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise UnregisteredSender(from_id)

    return {"merchant_id": str(row[0]), "status": row[1]}


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/webhooks/whatsapp")
async def whatsapp_verify(request: Request) -> Response:
    """
    Meta webhook verification handshake.

    Meta sends this GET request once when you save the webhook URL in the
    Meta dashboard. It verifies that the URL belongs to you before sending
    real events. If the verify_token matches, echo back hub.challenge.

    Parameters (query string):
      hub.mode       — always "subscribe"
      hub.verify_token — must match WHATSAPP_VERIFY_TOKEN from environment
      hub.challenge  — arbitrary value Meta wants echoed back as plain text

    Returns 200 + challenge on success, 403 on mismatch.
    """
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    expected_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")

    if mode == "subscribe" and token == expected_token:
        log.info("whatsapp_webhook_verified")
        return Response(content=challenge, media_type="text/plain", status_code=200)

    log.warning(
        "whatsapp_webhook_verify_failed",
        mode=mode,
        token_match=(token == expected_token),
    )
    return Response(status_code=403)


@router.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request) -> Response:
    """
    Receive an inbound WhatsApp message from a merchant via Meta Cloud API.

    Security: X-Hub-Signature-256 verified before any payload is read.
    An invalid signature returns 200 immediately (suppresses Meta retries)
    but is logged at warning level — same pattern as the Paystack webhook.

    Meta's payload shape:
      entry[0].changes[0].value.messages[0].from  — sender's WhatsApp ID (bare digits)
      entry[0].changes[0].value.messages[0].text.body — message text

    Flow:
      1. Verify signature.
      2. Extract sender ID and message text.
      3. Resolve sender → merchant.
      4. Parse PAY command.
      5. Create payment link.
      6. Reply with checkout URL.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not _verify_meta_signature(raw_body, signature):
        log.warning(
            "whatsapp_webhook_invalid_signature",
            source_ip=request.client.host if request.client else "unknown",
            has_signature_header=bool(signature),
        )
        # Return 200 to prevent Meta retrying a legitimately-rejected request
        return Response(status_code=200)

    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        log.error("whatsapp_webhook_invalid_json")
        return Response(status_code=200)

    # Extract message from Meta's nested payload structure
    try:
        entry = event.get("entry", [{}])[0]
        change = entry.get("changes", [{}])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])
    except (IndexError, AttributeError):
        # Not a message event (e.g. delivery receipt, status update) — ignore
        return Response(status_code=200)

    if not messages:
        # No messages in this event — could be a status update or other notification
        return Response(status_code=200)

    message = messages[0]
    if message.get("type") != "text":
        # Only handle text messages for now
        return Response(status_code=200)

    from_id = message.get("from", "")          # bare digits, e.g. "2348012345678"
    message_text = message.get("text", {}).get("body", "").strip()

    log.info("whatsapp_message_received", from_id=from_id, body_length=len(message_text))

    checkout_base = os.environ.get("CHECKOUT_BASE_URL", "http://localhost:8001")

    with get_conn() as conn:
        # Resolve sender to merchant
        try:
            merchant = resolve_merchant(conn, from_id)
        except UnregisteredSender:
            _send_whatsapp(
                from_id,
                "You're not registered with ConFam. "
                "Please contact support to get started.",
            )
            log.warning("whatsapp_unregistered_sender", from_id=from_id)
            return Response(status_code=200)

        merchant_id = merchant["merchant_id"]

        if merchant["status"] != "active":
            _send_whatsapp(
                from_id,
                "Your ConFam account is not yet active. "
                "Please complete account setup first.",
            )
            return Response(status_code=200)

        # Parse PAY command
        try:
            amount_minor_units, description = parse_pay_command(message_text)
        except ParseError as exc:
            _send_whatsapp(from_id, str(exc))
            return Response(status_code=200)

        # Create payment link (in-process — no second HTTP hop)
        try:
            link = create_link(
                conn=conn,
                merchant_id=merchant_id,
                amount_minor_units=amount_minor_units,
                currency="NGN",
                description=description,
            )
        except LinkValidationError as exc:
            _send_whatsapp(from_id, f"Could not create link: {exc}")
            log.error("messaging_link_creation_failed", merchant_id=merchant_id, error=str(exc))
            return Response(status_code=200)

    checkout_url = f"{checkout_base.rstrip('/')}/{link.link_id}"
    reply = (
        f"Payment link created ✅\n\n"
        f"Amount: ₦{amount_minor_units / 100:,.2f}\n"
        f"Item: {description}\n\n"
        f"Share this link with your buyer:\n{checkout_url}\n\n"
        f"The link expires in 30 minutes."
    )
    _send_whatsapp(from_id, reply)

    log.info(
        "payment_link_created_via_whatsapp",
        merchant_id=merchant_id,
        link_id=link.link_id,
        amount_minor_units=amount_minor_units,
    )

    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Payment confirmation notification (called by settlement engine)
# ---------------------------------------------------------------------------

def send_payment_confirmed_notification(
    merchant_confam_thread_id: str,
    amount_minor_units: int,
    link_id: str,
) -> None:
    """
    Send a WhatsApp payment-confirmation message to the merchant.

    Called by the settlement engine's webhook handler after a LedgerEntry
    is written.

    merchant_confam_thread_id: bare digits format for Meta Cloud API,
    e.g. "2348012345678". Must match what is stored in merchants.confam_thread_id.

    Rule 10: if Meta Cloud API is unreachable, the error is logged but
    settlement is NOT re-triggered — the ledger entry is already written.
    """
    naira = amount_minor_units / 100
    message = (
        f"Payment received ✅\n\n"
        f"Amount: ₦{naira:,.2f}\n"
        f"Paystack is processing settlement to your bank account.\n\n"
        f"Reference: {link_id[:8]}..."
    )
    _send_whatsapp(merchant_confam_thread_id, message)
    log.info(
        "merchant_payment_notification_sent",
        confam_thread_id=merchant_confam_thread_id,
        amount_minor_units=amount_minor_units,
        link_id=link_id,
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    close_pool()


app = FastAPI(
    title="ConFam Messaging",
    description="Meta WhatsApp Cloud API integration for the merchant-facing ConFam thread.",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "messaging"}
