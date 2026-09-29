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
import time
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import structlog
from fastapi import APIRouter, FastAPI, Request, Response

from confam.db import close_pool, get_conn
from confam.links import LinkValidationError, create_link
from services.messaging.statement import (
    build_statement_pdf,
    fetch_ledger_rows,
    statement_filename,
)

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


def _meta_credentials() -> tuple[str, str]:
    """(phone_number_id, access_token) from the environment.

    Rule 8: WHATSAPP_ACCESS_TOKEN is read from environment only, never
    hardcoded or logged.
    """
    return (
        os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
        os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
    )


def _upload_document(filename: str, pdf_bytes: bytes) -> str:
    """Upload the PDF to Meta's media endpoint and return its media_id.

    WhatsApp can only send a document by referencing a media_id that Meta
    holds, so the file has to be uploaded first, in two steps:
      POST /{phone_number_id}/media          -> {"id": "<media_id>"}
      POST /{phone_number_id}/messages       -> {"type": "document", ...}

    The upload must be multipart/form-data with the file part named "file" and
    mime_type "application/pdf"; a JSON body cannot carry the document.

    Returns the media_id. Raises on any failure or a response without an "id"
    so the caller can fall back to plain text — a document message with a
    missing or wrong media_id is silently undeliverable to the merchant.
    """
    phone_number_id, access_token = _meta_credentials()
    if not phone_number_id or not access_token:
        log.error("whatsapp_credentials_not_set")
        raise RuntimeError("WhatsApp credentials not set")

    response = httpx.post(
        f"{META_API_BASE}/{META_API_VERSION}/{phone_number_id}/media",
        headers={"Authorization": f"Bearer {access_token}"},
        data={"messaging_product": "whatsapp", "type": "application/pdf"},
        files={"file": (filename, pdf_bytes, "application/pdf")},
        timeout=30.0,
    )
    response.raise_for_status()

    media_id = response.json().get("id")
    if not media_id:
        raise RuntimeError("Meta media upload returned no media_id")

    log.info("whatsapp_media_uploaded", filename=filename, byte_length=len(pdf_bytes))
    return media_id


def _send_document(to: str, media_id: str, filename: str) -> bool:
    """Send an already-uploaded document to `to` as a WhatsApp message.

    Rule 10: log and continue on failure — a failed document send must never
    crash the webhook handler that delivered the command.

    Returns True if Meta accepted the message, False otherwise. The caller
    needs the result to decide whether to fall back to a plain-text reply: the
    document is only useful to the merchant if the send actually lands.
    """
    phone_number_id, access_token = _meta_credentials()
    if not phone_number_id or not access_token:
        log.error("whatsapp_credentials_not_set")
        return False

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
                "type": "document",
                "document": {"id": media_id, "filename": filename},
            },
            timeout=30.0,
        )
        response.raise_for_status()
        log.info("whatsapp_document_sent", to=to, filename=filename)
        return True
    except Exception as exc:
        log.error("whatsapp_document_send_failed", to=to, error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Message parsing (unchanged from Twilio version)
# ---------------------------------------------------------------------------

class ParseError(ValueError):
    """Raised when the merchant's message doesn't match the PAY command format."""


USAGE_MESSAGE = (
    "Sorry, I didn't understand that.\n\n"
    "Available commands:\n"
    "  REGISTER <business name> — register your business\n"
    "  ONBOARD <account number> <bank name> — set up your payout account\n"
    "  UPDATE <account number> <bank name> — change payout account\n"
    "  PAY <amount in naira> <description> — create a payment link\n"
    "  LEDGER — get a PDF statement of your confirmed sales\n"
    "  CANCEL — cancel a pending account change\n\n"
    "Example:\n"
    "  PAY 750 Ankara fabric x2"
)

# Merchant statuses that can act on commands. Anything else (suspended, or any
# status added later) gets the support message — fail closed, not open.
ACTIONABLE_STATUSES = ("active", "pending_verification")

REGISTRATION_INSTRUCTIONS = (
    "You're not registered with ConFam yet.\n\n"
    "To get started, send:\n"
    "  REGISTER <your business name>\n\n"
    "Example:\n"
    "  REGISTER Adaeze Fashion Store"
)

FINISH_SETUP_MESSAGE = (
    "Finish setup first: send ONBOARD <account number> <bank name>\n\n"
    "Example: ONBOARD 0123456789 GTBank"
)

ALREADY_ONBOARDED_MESSAGE = (
    "Your payout account is already set. "
    "Changing it is a separate process.\n\n"
    "Send: UPDATE <account number> <bank name> to change it, "
    "or PAY <amount> <description> to create a payment link."
)

SUPPORT_MESSAGE = "Your ConFam account is not active. Please contact support."

RATE_LIMIT_MESSAGE = (
    "You've made several account verification attempts recently.\n\n"
    "Please wait a while before trying again — we check each account with our "
    "banking partner and there's a limit on how often we can do that."
)

VERIFY_FAILED_MESSAGE = (
    "We couldn't verify that account just now.\n\n"
    "Please try again later. If it keeps happening, contact ConFam support."
)

ACCOUNT_NOT_FOUND_MESSAGE = (
    "We couldn't verify that account.\n\n"
    "Please check the 10-digit account number and bank code and try again."
)

PENDING_HELP_MESSAGE = (
    "You're registered — one step left.\n\n"
    "Send: ONBOARD <account number> <bank name>\n"
    "Example: ONBOARD 0123456789 GTBank\n\n"
    "That's all we need before you can start taking payments."
)

# LEDGER / STATEMENT — a PDF sales statement, not a bank statement. The
# wording "confirmed sales" matches what a ledger entry actually proves (the
# buyer's payment was collected); see OQ-020 and the disclaimer inside the PDF.
LEDGER_COMMANDS = ("LEDGER", "STATEMENT")

LEDGER_EMPTY_MESSAGE = (
    "No confirmed sales yet — once you get your first payment, send LEDGER again."
)

LEDGER_PENDING_MESSAGE = (
    "You don't have any confirmed sales yet.\n\n" + FINISH_SETUP_MESSAGE
)

# Sent when the PDF cannot be produced or delivered. Plain text only, no
# internal error detail, so a Meta outage still leaves the merchant with
# something actionable rather than silence.
LEDGER_FALLBACK_MESSAGE = (
    "Sorry — I couldn't send your statement just now.\n\n"
    "Please try again in a few minutes. If it keeps failing, contact ConFam support."
)

LEDGER_RATE_LIMIT_MESSAGE = (
    "You've requested several statements recently.\n\n"
    "Please wait a while before asking for another one."
)


def _already_registered_message(status: str) -> str:
    """Reply when a sender who is already a merchant sends REGISTER again."""
    if status == "pending_verification":
        return f"You're already registered. Next step is ONBOARD.\n\n{FINISH_SETUP_MESSAGE}"
    return "You're already registered.\n\nSend: PAY <amount> <description>"


def _command_word(text: str) -> str:
    """The first word of a message, uppercased, for command dispatch.

    Commands are case-insensitive and tolerate arbitrary leading/trailing
    whitespace, so '  onboard  0123 058' and 'ONBOARD 0123 058' route alike.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.split()[0].upper()


# ---------------------------------------------------------------------------
# Per-sender rate limiting on bank account verification
# ---------------------------------------------------------------------------

# ONBOARD and UPDATE both call Paystack bank/resolve, which is metered and
# rate-limits aggressively. A merchant (or anyone replaying a captured webhook)
# could otherwise burn our quota with a handful of messages and leave every
# other merchant unable to onboard. Limits the verification attempts, not all
# traffic — a merchant can still send unlimited PAY commands.
#
# Same in-process sliding window as services/checkout/middleware.py: correct
# for the single-instance pilot, to be replaced with Redis or a WAF rule when
# the service runs on more than one instance (see OPEN_QUESTIONS.md OQ-025
# rate-limiting follow-up).

_RATE_LIMIT_WINDOW_SECONDS = 3600


def _resolve_rate_limit() -> int:
    try:
        return int(os.environ.get("RATE_LIMIT_ONBOARD_PER_HOUR", "5"))
    except (ValueError, TypeError):
        return 5


# {sender_id: deque of monotonic timestamps of recent verification attempts}
_RESOLVE_ATTEMPTS: dict[str, deque] = {}


def check_resolve_rate_limit(sender_id: str) -> bool:
    """
    Record a bank account verification attempt. Returns True if over the limit.

    The attempt is recorded even when it is rejected, so a sender that keeps
    hammering stays throttled instead of getting a fresh budget each time.
    """
    limit = _resolve_rate_limit()
    if limit <= 0:
        return False  # disabled

    now = time.monotonic()
    window = _RESOLVE_ATTEMPTS.setdefault(sender_id, deque())
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()

    if len(window) >= limit:
        log.warning(
            "onboard_rate_limit_exceeded",
            from_id=sender_id,
            limit=limit,
            window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
        )
        return True

    window.append(now)
    return False


# LEDGER / STATEMENT gets its own budget on the same in-process sliding window
# used for onboarding. Sharing _RESOLVE_ATTEMPTS would mean a merchant asking
# for statements could exhaust their account-verification allowance and lock
# themselves out of ONBOARD, so the two limits stay separate. Sending a
# statement costs a Meta media upload, which is metered — this stops a replayed
# webhook or a bored merchant burning that quota.
#
# Set RATE_LIMIT_LEDGER_PER_HOUR=0 to disable, same as the onboard limit.

_LEDGER_ATTEMPTS: dict[str, deque] = {}


def _ledger_rate_limit() -> int:
    try:
        return int(os.environ.get("RATE_LIMIT_LEDGER_PER_HOUR", "10"))
    except (ValueError, TypeError):
        return 10


def check_ledger_rate_limit(sender_id: str) -> bool:
    """Record a statement request. Returns True if over the limit.

    Same semantics as check_resolve_rate_limit: the attempt is recorded even
    when rejected, so a sender that keeps hammering stays throttled instead of
    earning a fresh budget on every retry.
    """
    limit = _ledger_rate_limit()
    if limit <= 0:
        return False  # disabled

    now = time.monotonic()
    window = _LEDGER_ATTEMPTS.setdefault(sender_id, deque())
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()

    if len(window) >= limit:
        log.warning(
            "ledger_rate_limit_exceeded",
            from_id=sender_id,
            limit=limit,
            window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
        )
        return True

    window.append(now)
    return False


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


def parse_register_command(text: str) -> tuple[str, str]:
    """
    Parse 'REGISTER <business name> [GHANA]' from the merchant's message.
    Returns (business_name, country) where country is 'nigeria' (default) or 'ghana'.

    The country suffix is case-insensitive and optional. If omitted, country
    defaults to 'nigeria' — no extra prompt, keeping the existing Nigeria flow
    unchanged. Ghana merchants append GHANA as the last word:
      REGISTER Adaeze Fashion Store          -> ('Adaeze Fashion Store', 'nigeria')
      REGISTER Kwame Textiles GHANA          -> ('Kwame Textiles', 'ghana')

    Design choice: suffix rather than a separate question step. This avoids
    breaking the Nigeria flow (the vast majority of merchants) — they see no
    change at all. Ghana merchants need only one extra word. A two-step question
    would add a round-trip for every merchant regardless of country.
    """
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or parts[0].upper() != "REGISTER":
        raise ParseError(USAGE_MESSAGE)
    rest = parts[1].strip()
    if not rest:
        raise ParseError(
            "Please include your business name.\n"
            "Example: REGISTER Adaeze Fashion Store\n"
            "Ghana merchants: REGISTER Kwame Textiles GHANA"
        )
    # Check for supported country suffix (last word, case-insensitive)
    country = "nigeria"
    words = rest.split()
    if words and words[-1].upper() == "GHANA":
        country = "ghana"
        rest = " ".join(words[:-1]).strip()
        if not rest:
            raise ParseError(
                "Please include your business name before GHANA.\n"
                "Example: REGISTER Kwame Textiles GHANA"
            )
    if len(rest) > 200:
        raise ParseError("Business name must be 200 characters or fewer.")
    return rest, country


def parse_onboard_command(text: str, keyword: str) -> tuple[str, str]:
    """
    Parse 'ONBOARD <account_number> <bank name>' or
          'UPDATE  <account_number> <bank name>'.
    Returns (account_number, bank_name_raw).

    bank_name_raw is everything after the account number — may be multiple words
    (e.g. "Guaranty Trust Bank", "GTBank", "Ghana Commercial Bank").
    The caller is responsible for resolving the name to a bank code via
    confam.paystack.resolve_bank_name().

    Raises ParseError on bad format.
    """
    parts = text.strip().split()
    if len(parts) < 3 or parts[0].upper() != keyword.upper():
        raise ParseError(
            f"Format: {keyword.upper()} <10-digit account number> <bank name>\n"
            f"Example: {keyword.upper()} 0123456789 GTBank\n\n"
            "Use the bank's full name or a short version — we'll find the right one."
        )
    account_number = parts[1]
    bank_name_raw = " ".join(parts[2:]).strip()
    if not account_number.isdigit() or len(account_number) != 10:
        raise ParseError("Account number must be exactly 10 digits.\nExample: 0123456789")
    if not bank_name_raw:
        raise ParseError(
            f"Format: {keyword.upper()} <account number> <bank name>\n"
            f"Example: {keyword.upper()} 0123456789 GTBank"
        )
    return account_number, bank_name_raw


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
      4. Route on (merchant status, command).
      5. Reply.

    Routing is per command, NOT gated on the merchant being active. A blanket
    `status != 'active'` check ahead of dispatch made ONBOARD unreachable for
    exactly the merchants who needed it — the command is what makes a merchant
    active, so gating it on already being active is a deadlock. See
    _route_command for the state x command matrix.
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
        return _route_command(conn, from_id, message_text, checkout_base)


def _route_command(conn, from_id: str, message_text: str, checkout_base: str) -> Response:
    """
    Resolve the sender and dispatch on (merchant status, command).

    The state x command matrix:

      unregistered : REGISTER -> create merchant (pending_verification)
                     anything else -> registration instructions
      pending      : ONBOARD -> run onboarding
                     PAY     -> tell them to finish setup
                     REGISTER-> already registered, next step is ONBOARD
                     UPDATE  -> no account yet, point at ONBOARD
                     CANCEL  -> nothing to cancel
                     else    -> short help
      active       : PAY     -> create payment link
                     ONBOARD -> already set, changing is a separate process
                     UPDATE  -> start a change (cooling-off)
                     CANCEL  -> cancel a pending change
                     REGISTER-> already registered
                     else    -> usage
      other        : everything -> contact support (fails closed)

    Split out from the webhook endpoint so the matrix is testable on its own.
    """
    cmd = _command_word(message_text)

    # Unregistered senders may only REGISTER.
    try:
        merchant = resolve_merchant(conn, from_id)
    except UnregisteredSender:
        if cmd == "REGISTER":
            try:
                business_name, country = parse_register_command(message_text)
            except ParseError as exc:
                _send_whatsapp(from_id, str(exc))
                return Response(status_code=200)
            return _handle_register_command(conn, from_id, business_name, country)
        _send_whatsapp(from_id, REGISTRATION_INSTRUCTIONS)
        log.warning("whatsapp_unregistered_sender", from_id=from_id)
        return Response(status_code=200)

    merchant_id = merchant["merchant_id"]
    status = merchant["status"]

    # Suspended, or any status we don't recognise: nothing is actionable,
    # including REGISTER. Fails closed rather than open.
    if status not in ACTIONABLE_STATUSES:
        log.warning("whatsapp_command_blocked_status", from_id=from_id, status=status, command=cmd)
        _send_whatsapp(from_id, SUPPORT_MESSAGE)
        return Response(status_code=200)

    if cmd == "REGISTER":
        _send_whatsapp(from_id, _already_registered_message(status))
        return Response(status_code=200)

    if status == "pending_verification":
        return _route_pending(conn, from_id, merchant_id, message_text, cmd)

    return _route_active(conn, from_id, merchant_id, message_text, cmd, checkout_base)


def _route_pending(conn, from_id, merchant_id, message_text, cmd) -> Response:
    """Commands available to a merchant who has registered but not onboarded."""
    if cmd == "ONBOARD":
        try:
            account_number, bank_name_raw = parse_onboard_command(message_text, "ONBOARD")
        except ParseError as exc:
            _send_whatsapp(from_id, str(exc))
            return Response(status_code=200)
        return _handle_onboard_command(conn, from_id, merchant_id, account_number, bank_name_raw)

    if cmd == "PAY":
        _send_whatsapp(from_id, FINISH_SETUP_MESSAGE)
        return Response(status_code=200)

    if cmd in LEDGER_COMMANDS:
        # A merchant who has not finished onboarding has no confirmed sales by
        # definition — they cannot take a payment yet. Say so, and point at the
        # step that unblocks them. No DB read is needed to know this.
        _send_whatsapp(from_id, LEDGER_PENDING_MESSAGE)
        return Response(status_code=200)

    if cmd == "UPDATE":
        # No account to change yet — don't spend a Paystack resolve to discover that.
        _send_whatsapp(
            from_id,
            "You don't have a payout account set up yet.\n\n" + FINISH_SETUP_MESSAGE,
        )
        return Response(status_code=200)

    if cmd in ("CANCEL", "STOP"):
        _send_whatsapp(
            from_id,
            "You don't have a pending payout account change to cancel. "
            "If you haven't finished setup, send ONBOARD <account number> <bank name>.",
        )
        return Response(status_code=200)

    _send_whatsapp(from_id, PENDING_HELP_MESSAGE)
    return Response(status_code=200)


def _merchant_currency(conn, merchant_id: str) -> str:
    """Return the ISO 4217 currency code for the merchant's country.

    Nigeria -> NGN, Ghana -> GHS.
    Defaults to NGN for any unrecognised country value so existing merchants
    are never broken by a missing or unexpected DB value.
    """
    _COUNTRY_CURRENCY = {"nigeria": "NGN", "ghana": "GHS"}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT country FROM merchants WHERE merchant_id = %s",
            (merchant_id,),
        )
        row = cur.fetchone()
    country = row[0] if row else "nigeria"
    return _COUNTRY_CURRENCY.get(country, "NGN")


def _merchant_currency(conn, merchant_id: str) -> str:
    """Return the ISO 4217 currency code for the merchant's country.

    Nigeria -> NGN, Ghana -> GHS.
    Defaults to NGN for any unrecognised country value so existing merchants
    are never broken by a missing or unexpected DB value.
    """
    _COUNTRY_CURRENCY = {"nigeria": "NGN", "ghana": "GHS"}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT country FROM merchants WHERE merchant_id = %s",
            (merchant_id,),
        )
        row = cur.fetchone()
    country = row[0] if row else "nigeria"
    return _COUNTRY_CURRENCY.get(country, "NGN")


def _route_active(conn, from_id, merchant_id, message_text, cmd, checkout_base) -> Response:
    """Commands available to a fully onboarded merchant."""
    if cmd == "ONBOARD":
        # Must not create or overwrite a second payout account. The API returns
        # 409 for the same case; changing accounts goes through UPDATE.
        _send_whatsapp(from_id, ALREADY_ONBOARDED_MESSAGE)
        return Response(status_code=200)

    if cmd in ("CANCEL", "STOP"):
        return _handle_cancel_command(conn, from_id, merchant_id)

    if cmd == "UPDATE":
        try:
            account_number, bank_name_raw = parse_onboard_command(message_text, "UPDATE")
        except ParseError as exc:
            _send_whatsapp(from_id, str(exc))
            return Response(status_code=200)
        return _handle_update_command(conn, from_id, merchant_id, account_number, bank_name_raw)

    if cmd in LEDGER_COMMANDS:
        return _handle_ledger_command(conn, from_id, merchant_id)

    # Parse PAY command
    try:
        amount_minor_units, description = parse_pay_command(message_text)
    except ParseError as exc:
        _send_whatsapp(from_id, str(exc))
        return Response(status_code=200)

    # Create payment link (in-process — no second HTTP hop)
    try:
        currency = _merchant_currency(conn, merchant_id)
        link = create_link(
            conn=conn,
            merchant_id=merchant_id,
            amount_minor_units=amount_minor_units,
            currency=currency,
            description=description,
        )
    except LinkValidationError as exc:
        _send_whatsapp(from_id, f"Could not create link: {exc}")
        log.error("messaging_link_creation_failed", merchant_id=merchant_id, error=str(exc))
        return Response(status_code=200)

    checkout_url = f"{checkout_base.rstrip('/')}/{link.link_id}"
    _CURRENCY_SYMBOL = {"NGN": "₦", "GHS": "GH₵"}
    symbol = _CURRENCY_SYMBOL.get(currency, currency + " ")
    reply = (
        f"Payment link created ✅\n\n"
        f"Amount: {symbol}{amount_minor_units / 100:,.2f}\n"
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
# LEDGER / STATEMENT command (merchant sales statement PDF)
# ---------------------------------------------------------------------------


def _fetch_business_name(conn, merchant_id: str) -> str:
    """Business name for the statement header.

    Scoped by merchant_id, so a statement can never be headed with another
    merchant's name. An empty string is a valid result — the PDF falls back to
    "Unnamed business" rather than failing the whole document over a header.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT business_name FROM merchants WHERE merchant_id = %s",
            (merchant_id,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else ""


def _handle_ledger_command(conn, from_id: str, merchant_id: str) -> Response:
    """LEDGER / STATEMENT — send a PDF of the merchant's confirmed sales.

    Delivery is a two-step Meta flow: upload the PDF to /media to get a
    media_id, then send a document message referencing it. Both steps can
    fail independently, and either failure leaves the merchant with nothing,
    so both are answered with a plain-text fallback that tells them to retry
    (Rule 10).

    Read-only throughout: this handler SELECTs the ledger and sends a
    document. It never writes a ledger row, and requesting a statement is not
    recorded anywhere.
    """
    if check_ledger_rate_limit(from_id):
        _send_whatsapp(from_id, LEDGER_RATE_LIMIT_MESSAGE)
        return Response(status_code=200)

    try:
        rows, total_count = fetch_ledger_rows(conn, merchant_id)
    except Exception as exc:
        log.error("ledger_fetch_failed", merchant_id=merchant_id, error=str(exc))
        _send_whatsapp(from_id, LEDGER_FALLBACK_MESSAGE)
        return Response(status_code=200)

    if not rows:
        log.info("ledger_requested_empty", merchant_id=merchant_id, from_id=from_id)
        _send_whatsapp(from_id, LEDGER_EMPTY_MESSAGE)
        return Response(status_code=200)

    generated_at = datetime.now(UTC)

    try:
        business_name = _fetch_business_name(conn, merchant_id)
        pdf_bytes = build_statement_pdf(business_name, rows, total_count, generated_at)
        filename = statement_filename(business_name, generated_at)
    except Exception as exc:
        log.error("ledger_pdf_build_failed", merchant_id=merchant_id, error=str(exc))
        _send_whatsapp(from_id, LEDGER_FALLBACK_MESSAGE)
        return Response(status_code=200)

    try:
        media_id = _upload_document(filename, pdf_bytes)
    except Exception as exc:
        log.error(
            "ledger_media_upload_failed",
            merchant_id=merchant_id,
            filename=filename,
            error=str(exc),
        )
        _send_whatsapp(from_id, LEDGER_FALLBACK_MESSAGE)
        return Response(status_code=200)

    if not _send_document(from_id, media_id, filename):
        _send_whatsapp(from_id, LEDGER_FALLBACK_MESSAGE)
        return Response(status_code=200)

    log.info(
        "ledger_statement_sent",
        merchant_id=merchant_id,
        filename=filename,
        rows_rendered=len(rows),
        total_count=total_count,
        truncated=total_count > len(rows),
    )
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Payment confirmation notification (called by settlement engine)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CANCEL command handler (OQ-025)
# ---------------------------------------------------------------------------


def _handle_register_command(conn, from_id: str, business_name: str, country: str = "nigeria") -> Response:
    """
    REGISTER <business name> [GHANA] — create a new merchant account.

    Creates a merchant with status=pending_verification and confam_thread_id
    set to the sender's WhatsApp ID (bare digits). country defaults to 'nigeria';
    Ghana merchants pass 'ghana'. The merchant can then use ONBOARD to add
    their bank account.
    """
    country_line = f"\nCountry: Ghana" if country == "ghana" else ""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO merchants (whatsapp_number, confam_thread_id, business_name, status, country)
                   VALUES (%s, %s, %s, 'pending_verification', %s) RETURNING merchant_id""",
                (f"+{from_id}", from_id, business_name, country),
            )
            merchant_id = str(cur.fetchone()[0])
        conn.commit()
        _send_whatsapp(
            from_id,
            f"✅ Business registered!\n\n"
            f"Business: {business_name}{country_line}\n\n"
            f"Next step — set up your payout account:\n"
            f"Send: ONBOARD <account number> <bank name>\n"
            f"Example: ONBOARD 0123456789 GTBank",
        )
        log.info("merchant_registered_via_whatsapp", from_id=from_id, merchant_id=merchant_id, country=country)
    except Exception as exc:
        if "unique" in str(exc).lower():
            # Lost a race with a concurrent REGISTER from the same sender.
            conn.rollback()
            _send_whatsapp(from_id, "You're already registered.\n\n" + FINISH_SETUP_MESSAGE)
        else:
            conn.rollback()
            log.error("register_command_failed", from_id=from_id, error=str(exc))
            _send_whatsapp(from_id, "Registration failed. Please try again or contact support.")
    return Response(status_code=200)


def _handle_onboard_command(
    conn, from_id: str, merchant_id: str, account_number: str, bank_name_raw: str
) -> Response:
    """
    ONBOARD <account_number> <bank name> — first-time payout account setup.

    1. Resolves the typed bank name to a Paystack bank code via the cached
       bank list for the merchant's country (Nigeria/Ghana).
    2. Calls Paystack bank/resolve to verify the account number.
    3. Creates a Paystack subaccount.
    4. Writes the PayoutAccount row and activates the merchant.
    """
    from confam.payout_accounts import NoActivePayoutAccount, get_active_payout_account
    from confam.paystack import (
        BankNameNotResolved,
        PaystackError,
        PaystackRateLimited,
        create_subaccount,
        resolve_bank_account,
        resolve_bank_name,
    )

    # A merchant that already onboarded must not get a second account.
    try:
        get_active_payout_account(conn, merchant_id)
    except NoActivePayoutAccount:
        pass
    else:
        _send_whatsapp(from_id, ALREADY_ONBOARDED_MESSAGE)
        log.info("onboard_rejected_already_active", from_id=from_id, merchant_id=merchant_id)
        return Response(status_code=200)

    # Each ONBOARD spends Paystack bank/resolve quota — rate limit per sender.
    if check_resolve_rate_limit(from_id):
        _send_whatsapp(from_id, RATE_LIMIT_MESSAGE)
        return Response(status_code=200)

    # Get merchant's country to look up the correct bank list.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT country, business_name FROM merchants WHERE merchant_id = %s",
            (merchant_id,),
        )
        row = cur.fetchone()
    merchant_country = row[0] if row else "nigeria"
    business_name_db = row[1] if row else None

    # Resolve the typed bank name to a Paystack bank code.
    try:
        bank_code, resolved_bank_display = resolve_bank_name(bank_name_raw, merchant_country)
    except BankNameNotResolved as exc:
        candidates_text = ""
        if exc.candidates:
            names = "\n".join(f"  • {c.name}" for c in exc.candidates[:3])
            candidates_text = (
                f"\n\nDid you mean one of these?\n{names}\n\n"
                f"Send the full name exactly as shown above."
            )
        _send_whatsapp(
            from_id,
            f"We couldn't find a bank named '{bank_name_raw}'.{candidates_text}\n\n"
            f"Try: ONBOARD {account_number} <exact bank name>",
        )
        log.warning(
            "onboard_bank_name_not_resolved",
            from_id=from_id,
            merchant_id=merchant_id,
            bank_name=bank_name_raw,
            country=merchant_country,
        )
        return Response(status_code=200)
    except PaystackError as exc:
        _send_whatsapp(from_id, VERIFY_FAILED_MESSAGE)
        log.warning(
            "onboard_bank_list_failed",
            from_id=from_id,
            merchant_id=merchant_id,
            error=str(exc),
        )
        return Response(status_code=200)

    # Verify bank account with Paystack
    try:
        resolved = resolve_bank_account(account_number=account_number, bank_code=bank_code)
    except PaystackRateLimited:
        _send_whatsapp(from_id, VERIFY_FAILED_MESSAGE)
        log.warning("onboard_paystack_rate_limited", from_id=from_id, merchant_id=merchant_id)
        return Response(status_code=200)
    except PaystackError as exc:
        _send_whatsapp(from_id, ACCOUNT_NOT_FOUND_MESSAGE)
        log.warning(
            "onboard_resolution_failed",
            from_id=from_id,
            merchant_id=merchant_id,
            reason=str(exc),
        )
        return Response(status_code=200)

    business_name = business_name_db or resolved.account_name

    try:
        subaccount = create_subaccount(
            business_name=business_name,
            bank_code=bank_code,
            account_number=account_number,
            percentage_charge=0.0,
        )
    except PaystackRateLimited:
        _send_whatsapp(from_id, VERIFY_FAILED_MESSAGE)
        log.warning("onboard_subaccount_rate_limited", from_id=from_id, merchant_id=merchant_id)
        return Response(status_code=200)
    except PaystackError as exc:
        _send_whatsapp(from_id, "❌ Account setup failed. Please try again or contact support.")
        log.error(
            "onboard_subaccount_failed", from_id=from_id, merchant_id=merchant_id, reason=str(exc)
        )
        return Response(status_code=200)

    # Write PayoutAccount and activate merchant
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO payout_accounts (
                    merchant_id, bank_account_number, bank_code, account_holder_name,
                    verification_method, verified_at, active_from, paystack_subaccount_code
                ) VALUES (%s, %s, %s, %s, 'bank_api_resolve', now(), now(), %s)""",
                (
                    merchant_id,
                    account_number,
                    bank_code,
                    resolved.account_name,
                    subaccount.subaccount_code,
                ),
            )
            cur.execute(
                "UPDATE merchants SET status = 'active' WHERE merchant_id = %s",
                (merchant_id,),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if "unique" in str(exc).lower():
            _send_whatsapp(
                from_id,
                "You already have an active payout account.\n"
                "To change it, send: UPDATE <account number> <bank name>",
            )
        else:
            log.error("onboard_db_write_failed", from_id=from_id, error=str(exc))
            _send_whatsapp(from_id, "❌ Setup failed. Please try again or contact support.")
        return Response(status_code=200)

    masked = "*" * (len(account_number) - 4) + account_number[-4:]
    _send_whatsapp(
        from_id,
        f"✅ Payout account set up!\n\n"
        f"Bank: {resolved_bank_display}\n"
        f"Account holder: {resolved.account_name}\n"
        f"Account: {masked}\n\n"
        f"You're ready to accept payments.\n"
        f"Send: PAY <amount> <description>\n"
        f"Example: PAY 750 Ankara fabric x2",
    )
    log.info("onboard_command_succeeded", from_id=from_id, merchant_id=merchant_id)
    return Response(status_code=200)


def _handle_update_command(
    conn, from_id: str, merchant_id: str, account_number: str, bank_name_raw: str
) -> Response:
    """
    UPDATE <account_number> <bank name> — change payout account with cooling-off.
    Resolves bank name to code, verifies account, sends immediate notification.
    Merchant can reply CANCEL to abort during the cooling-off window.
    """
    from confam.payout_accounts import (
        NoActivePayoutAccount,
        PendingChangeAlreadyExists,
        request_payout_account_change,
    )
    from confam.paystack import (
        BankNameNotResolved,
        PaystackError,
        PaystackRateLimited,
        create_subaccount,
        resolve_bank_account,
        resolve_bank_name,
    )

    # UPDATE spends the same Paystack bank/resolve quota as ONBOARD — same limit.
    if check_resolve_rate_limit(from_id):
        _send_whatsapp(from_id, RATE_LIMIT_MESSAGE)
        return Response(status_code=200)

    # Get merchant's country for the correct bank list.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT country, business_name FROM merchants WHERE merchant_id = %s",
            (merchant_id,),
        )
        row = cur.fetchone()
    merchant_country = row[0] if row else "nigeria"
    business_name_db = row[1] if row else None

    # Resolve bank name to code.
    try:
        bank_code, resolved_bank_display = resolve_bank_name(bank_name_raw, merchant_country)
    except BankNameNotResolved as exc:
        candidates_text = ""
        if exc.candidates:
            names = "\n".join(f"  • {c.name}" for c in exc.candidates[:3])
            candidates_text = (
                f"\n\nDid you mean one of these?\n{names}\n\n"
                f"Send the full name exactly as shown above."
            )
        _send_whatsapp(
            from_id,
            f"We couldn't find a bank named '{bank_name_raw}'.{candidates_text}\n\n"
            f"Try: UPDATE {account_number} <exact bank name>",
        )
        log.warning(
            "update_bank_name_not_resolved",
            from_id=from_id,
            merchant_id=merchant_id,
            bank_name=bank_name_raw,
        )
        return Response(status_code=200)
    except PaystackError as exc:
        _send_whatsapp(from_id, VERIFY_FAILED_MESSAGE)
        log.warning("update_bank_list_failed", from_id=from_id, error=str(exc))
        return Response(status_code=200)

    try:
        resolved = resolve_bank_account(account_number=account_number, bank_code=bank_code)
    except PaystackRateLimited:
        _send_whatsapp(from_id, VERIFY_FAILED_MESSAGE)
        return Response(status_code=200)
    except PaystackError as exc:
        _send_whatsapp(from_id, ACCOUNT_NOT_FOUND_MESSAGE)
        log.warning(
            "update_resolution_failed", from_id=from_id, merchant_id=merchant_id, reason=str(exc)
        )
        return Response(status_code=200)

    business_name = business_name_db or resolved.account_name

    try:
        subaccount = create_subaccount(
            business_name=business_name, bank_code=bank_code,
            account_number=account_number, percentage_charge=0.0,
        )
    except PaystackRateLimited:
        _send_whatsapp(from_id, VERIFY_FAILED_MESSAGE)
        return Response(status_code=200)
    except PaystackError as exc:
        _send_whatsapp(from_id, "❌ Update failed. Please try again or contact support.")
        log.error(
            "update_subaccount_failed", from_id=from_id, merchant_id=merchant_id, reason=str(exc)
        )
        return Response(status_code=200)

    try:
        request_payout_account_change(
            conn, merchant_id=merchant_id,
            bank_account_number=account_number, bank_code=bank_code,
            account_holder_name=resolved.account_name,
            paystack_subaccount_code=subaccount.subaccount_code,
        )
    except NoActivePayoutAccount:
        _send_whatsapp(
            from_id,
            "You don't have a payout account set up yet.\n"
            "Send: ONBOARD <account number> <bank name>",
        )
        return Response(status_code=200)
    except PendingChangeAlreadyExists:
        _send_whatsapp(
            from_id,
            "⚠️ You already have a pending account change.\n"
            "Reply CANCEL to cancel it first, then send UPDATE again.",
        )
        return Response(status_code=200)

    import os as _os
    cooling = int(_os.environ.get("PAYOUT_ACCOUNT_COOLING_OFF_SECONDS", "172800")) // 3600
    masked = "*" * (len(account_number) - 4) + account_number[-4:]
    _send_whatsapp(
        from_id,
        f"⚠️ Payout account change requested\n\n"
        f"Bank: {resolved_bank_display}\n"
        f"New account: {masked} ({resolved.account_name})\n"
        f"Takes effect in: {cooling} hours\n\n"
        f"If this wasn't you, reply CANCEL immediately.",
    )
    log.info("update_command_succeeded", from_id=from_id, merchant_id=merchant_id)
    return Response(status_code=200)


def _handle_cancel_command(conn, from_id: str, merchant_id: str) -> Response:
    """
    Handle a CANCEL or STOP reply from a merchant during the cooling-off window.

    If a pending payout account change exists, cancel it and confirm to the
    merchant. If there is nothing to cancel, inform them politely.

    This closes OQ-025: WhatsApp-based cancellation of a pending change,
    in addition to the existing API endpoint
    (POST /merchants/{id}/payout-account/change/cancel).
    """
    from confam.payout_accounts import (
        NoPendingChange,
        cancel_pending_change,
        get_pending_change,
    )

    pending = get_pending_change(conn, merchant_id)
    if pending is None:
        _send_whatsapp(
            from_id,
            "You don't have a pending payout account change to cancel. "
            "If you recently made a change request and it didn't arrive, "
            "please contact ConFam support.",
        )
        log.info("cancel_command_no_pending_change", from_id=from_id, merchant_id=merchant_id)
        return Response(status_code=200)

    try:
        cancel_pending_change(conn, merchant_id)
        _send_whatsapp(
            from_id,
            "✅ Payout account change cancelled.\n\n"
            "Your existing bank account remains active. "
            "No changes were made.",
        )
        log.info("cancel_command_succeeded", from_id=from_id, merchant_id=merchant_id)
    except NoPendingChange:
        # Race condition — change activated between get and cancel
        _send_whatsapp(
            from_id,
            "The pending change was already processed and is now active. "
            "If you did not authorise this, contact ConFam support immediately.",
        )
        log.warning("cancel_command_race_condition", from_id=from_id, merchant_id=merchant_id)

    return Response(status_code=200)


def send_payment_confirmed_notification(
    merchant_confam_thread_id: str,
    amount_minor_units: int,
    link_id: str,
    currency: str = "NGN",
) -> None:
    """
    Send a WhatsApp payment-confirmation message to the merchant.

    Called by the settlement engine's webhook handler after a LedgerEntry
    is written.

    merchant_confam_thread_id: bare digits format for Meta Cloud API,
    e.g. "2348012345678". Must match what is stored in merchants.confam_thread_id.

    currency: ISO 4217 code — NGN for Nigeria, GHS for Ghana. Defaults to NGN
    for backward compatibility with existing callers that omit this argument.

    Rule 10: if Meta Cloud API is unreachable, the error is logged but
    settlement is NOT re-triggered — the ledger entry is already written.
    """
    _CURRENCY_SYMBOL = {"NGN": "₦", "GHS": "GH₵"}
    symbol = _CURRENCY_SYMBOL.get(currency, currency + " ")
    amount_display = amount_minor_units / 100
    message = (
        f"Payment received ✅\n\n"
        f"Amount: {symbol}{amount_display:,.2f}\n"
        f"Paystack is processing settlement to your bank account.\n\n"
        f"Reference: {link_id[:8]}..."
    )
    _send_whatsapp(merchant_confam_thread_id, message)
    log.info(
        "merchant_payment_notification_sent",
        confam_thread_id=merchant_confam_thread_id,
        amount_minor_units=amount_minor_units,
        currency=currency,
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
