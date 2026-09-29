"""
confam.interactive — WhatsApp interactive message helpers and common-bank config.

Owns:
  - COMMON_BANKS_BY_COUNTRY: the curated list of banks shown in the ONBOARD
    list message (up to 9 per country, leaving one slot for "Other").
  - PENDING_BANK_SELECTION_TTL_SECONDS: how long a tapped bank selection stays
    valid while we wait for the merchant's account number.
  - send_register_country_buttons(): reply-buttons message asking Nigeria/Ghana.
  - send_onboard_bank_list(): list message showing ~9 common banks + "Other".
  - Button/list reply ID encoding and decoding.
  - DB helpers: set/clear/read pending bank selection.

What this module does NOT do:
  - No payment logic.
  - No ledger writes.
  - No Paystack calls (those stay in confam.paystack).

## Common-bank list design notes
The Paystack GET /bank response is sorted by bank `id` descending (most
recently added first — confirmed from the sample response in the API docs).
It is NOT sorted by popularity. A raw slice of that list would surface obscure
recently-added microfinance banks above GTBank and Access Bank. Therefore
COMMON_BANKS_BY_COUNTRY is an explicit curated list, not a slice of the API.

IMPORTANT: These lists are a reasonable starting guess based on publicly
available market-share data, but they are a product and local-market judgment
call — not a technical fact. They should be reviewed against what your actual
test merchants use before launch. Edit freely; the structure is the same
regardless of which banks appear.

## Ghana mobile money inclusion rationale
Paystack Ghana supports two channel types:
  - ghipps   — traditional bank accounts (GhIPPS inter-bank rails)
  - mobile_money — MTN MoMo, AirtelTigo Cash, Vodafone Cash

For Ghanaian merchants, mobile money is meaningfully more common than
traditional bank accounts as a settlement destination. Paystack's compliance
page explicitly accepts a "personal mobile money number" as a settlement
account. The /bank/resolve endpoint and subaccount creation work identically
for mobile_money channels as for ghipps — the bank_code simply differs
(e.g. "MTN" for MTN MoMo). Therefore mobile money entries are included in
the Ghana common-bank list. No code divergence is needed.

The Ghana account number format for BOTH ghipps and mobile_money is 10 digits:
  - ghipps: 10-digit bank account number (same length as Nigerian NUBAN)
  - mobile_money: 10-digit subscriber phone number (e.g. 0241234567 for MTN)
The plausibility check in messaging/main.py uses exactly 10 digits for both.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import structlog

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Pending bank selection TTL
# ---------------------------------------------------------------------------

# How long a bank tap selection stays valid. After this, a bare account number
# arriving from the merchant is treated as normal text, not as completing an
# ONBOARD flow. Named constant — do not use a magic number in the logic.
PENDING_BANK_SELECTION_TTL_SECONDS = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Common-bank lists
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommonBank:
    """A bank entry for the interactive list message."""
    name: str   # Display name in the list row (≤24 chars recommended by Meta)
    code: str   # Paystack bank code — used directly as the row ID payload


# REVIEW BEFORE LAUNCH: These lists are a starting guess, not a finalized
# product decision. Review against what your actual test merchants use.
# Max 9 entries per country (leaving slot 10 for "Other — type your bank name").
#
# Nigeria: sorted by estimated merchant popularity, not alphabetically.
# Sources: CBN market share data, Paystack community usage patterns.
#
# Ghana: mix of traditional banks (ghipps) and mobile money (mobile_money)
# because mobile money is the dominant settlement channel for small Ghanaian
# merchants. Paystack /bank/resolve works identically for both types.
# The bank_code for mobile money is the operator code Paystack uses
# (verify these codes against your live Paystack Ghana account's /bank list).

COMMON_BANKS_BY_COUNTRY: dict[str, list[CommonBank]] = {
    "nigeria": [
        CommonBank("GTBank",               "058"),
        CommonBank("Access Bank",          "044"),
        CommonBank("Zenith Bank",          "057"),
        CommonBank("First Bank",           "011"),
        CommonBank("UBA",                  "033"),
        CommonBank("Fidelity Bank",        "070"),
        CommonBank("Sterling Bank",        "232"),
        CommonBank("Kuda Bank",            "090267"),
        CommonBank("Opay",                 "100004"),
    ],
    "ghana": [
        # Mobile money — dominant for small merchants
        CommonBank("MTN MoMo",             "MTN"),
        CommonBank("AirtelTigo Money",     "ATL"),
        CommonBank("Vodafone Cash",        "VOD"),
        # Traditional banks (ghipps)
        CommonBank("GCB Bank",             "GCB"),
        CommonBank("Fidelity Bank Ghana",  "FBG"),
        CommonBank("Ecobank Ghana",        "ECO"),
        CommonBank("Absa Bank Ghana",      "ABS"),
        CommonBank("Stanbic Bank Ghana",   "SBG"),
        CommonBank("Access Bank Ghana",    "ACG"),
    ],
}

# The "Other" row that always appears as the last item in the list.
# Tapping it sends the merchant back to the typed ONBOARD flow.
_OTHER_ROW_ID = "bank:other"
_OTHER_ROW_TITLE = "Other — type bank name"
_OTHER_ROW_DESCRIPTION = "Send: ONBOARD <account> <bank name>"


# ---------------------------------------------------------------------------
# Button/list reply ID encoding
# ---------------------------------------------------------------------------

# ID prefixes — used for routing inbound interactive replies
REGISTER_ID_PREFIX = "register:"
BANK_ID_PREFIX = "bank:"

# Country codes embedded in register button IDs
_COUNTRY_ID_MAP = {"nigeria": "NG", "ghana": "GH"}
_ID_COUNTRY_MAP = {v: k for k, v in _COUNTRY_ID_MAP.items()}

# Meta's hard limit on interactive reply id length
_META_MAX_ID_LEN = 256


def encode_register_id(country: str, business_name: str) -> str:
    """Build the button reply id for a REGISTER country selection.

    Format: register:<CC>:<business_name>
    e.g.  : register:NG:Adaeze Fashion Store

    If the encoded id would exceed Meta's 256-char limit, truncate the
    business name with a marker so it's still recoverable on decode.
    """
    cc = _COUNTRY_ID_MAP.get(country, "NG")
    prefix = f"{REGISTER_ID_PREFIX}{cc}:"
    max_name_len = _META_MAX_ID_LEN - len(prefix)
    if len(business_name) > max_name_len:
        truncated = business_name[:max_name_len - 3] + "..."
        log.warning(
            "register_id_business_name_truncated",
            original_len=len(business_name),
            truncated_len=len(truncated),
        )
        return prefix + truncated
    return prefix + business_name


def decode_register_id(reply_id: str) -> tuple[str, str] | None:
    """Parse a register button reply id back to (country, business_name).

    Returns None if the id doesn't match the expected format.
    """
    if not reply_id.startswith(REGISTER_ID_PREFIX):
        return None
    rest = reply_id[len(REGISTER_ID_PREFIX):]
    parts = rest.split(":", 1)
    if len(parts) != 2:
        return None
    cc, name = parts
    country = _ID_COUNTRY_MAP.get(cc)
    if not country or not name:
        return None
    return country, name


def encode_bank_id(bank_code: str) -> str:
    """Build the list reply id for a bank row selection.

    Format: bank:<code>   e.g.  bank:058
    """
    return f"{BANK_ID_PREFIX}{bank_code}"


def decode_bank_id(reply_id: str) -> str | None:
    """Parse a bank list reply id back to a bank_code.

    Returns None if the id doesn't match, or "other" for the Other row.
    """
    if not reply_id.startswith(BANK_ID_PREFIX):
        return None
    code = reply_id[len(BANK_ID_PREFIX):]
    return code if code else None


# ---------------------------------------------------------------------------
# DB helpers: pending bank selection
# ---------------------------------------------------------------------------

def set_pending_bank_selection(conn, merchant_id: str, bank_code: str) -> None:
    """Record that the merchant tapped a bank code, awaiting their account number."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE merchants
                  SET pending_bank_code = %s,
                      pending_bank_selected_at = now()
                WHERE merchant_id = %s""",
            (bank_code, merchant_id),
        )
    conn.commit()


def clear_pending_bank_selection(conn, merchant_id: str) -> None:
    """Clear the pending selection after onboarding completes or expires."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE merchants
                  SET pending_bank_code = NULL,
                      pending_bank_selected_at = NULL
                WHERE merchant_id = %s""",
            (merchant_id,),
        )
    conn.commit()


def get_pending_bank_selection(
    conn, merchant_id: str
) -> tuple[str, datetime] | None:
    """Return (bank_code, selected_at) if there is a non-expired pending selection.

    Returns None if there is no pending selection or it has expired.
    Does NOT automatically clear an expired selection — leave that to the
    caller so we don't mask expiry in tests.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT pending_bank_code, pending_bank_selected_at
                 FROM merchants
                WHERE merchant_id = %s""",
            (merchant_id,),
        )
        row = cur.fetchone()

    if row is None or row[0] is None or row[1] is None:
        return None

    bank_code, selected_at = row
    # Normalise tz
    if selected_at.tzinfo is None:
        selected_at = selected_at.replace(tzinfo=UTC)

    age_seconds = (datetime.now(UTC) - selected_at).total_seconds()
    if age_seconds > PENDING_BANK_SELECTION_TTL_SECONDS:
        return None

    return bank_code, selected_at


# ---------------------------------------------------------------------------
# Outbound interactive message senders
# ---------------------------------------------------------------------------

def _meta_credentials() -> tuple[str, str]:
    """(phone_number_id, access_token) from environment (Rule 8)."""
    return (
        os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
        os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
    )


def send_register_country_buttons(to: str, business_name: str) -> bool:
    """Send a reply-buttons message asking the merchant which country they're in.

    Buttons:
      [Nigeria]  id = register:NG:<business_name>
      [Ghana]    id = register:GH:<business_name>

    Returns True if Meta accepted the request, False on failure.
    Falls back gracefully — the caller must handle False by sending a
    plain-text prompt instead (Rule 10).
    """
    phone_number_id, access_token = _meta_credentials()
    if not phone_number_id or not access_token:
        log.error("whatsapp_credentials_not_set")
        return False

    ng_id = encode_register_id("nigeria", business_name)
    gh_id = encode_register_id("ghana", business_name)

    # Meta button title max 20 chars; these are well within limit.
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    f"Which country is {business_name} based in?\n\n"
                    "Tap to select, or reply NIGERIA or GHANA."
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": ng_id, "title": "🇳🇬 Nigeria"}},
                    {"type": "reply", "reply": {"id": gh_id, "title": "🇬🇭 Ghana"}},
                ]
            },
        },
    }

    try:
        META_API_VERSION = "v20.0"
        META_API_BASE = "https://graph.facebook.com"
        response = httpx.post(
            f"{META_API_BASE}/{META_API_VERSION}/{phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        log.info("register_country_buttons_sent", to=to, business_name=business_name)
        return True
    except Exception as exc:
        log.error("register_country_buttons_send_failed", to=to, error=str(exc))
        return False


def send_onboard_bank_list(to: str, country: str) -> bool:
    """Send a list message with up to 9 common banks + "Other" for the given country.

    The 10-row hard limit (WhatsApp) is respected:
      - Up to 9 common banks from COMMON_BANKS_BY_COUNTRY[country]
      - 1 final row: "Other — type your bank name"

    Each bank row's id encodes the bank code directly (e.g. bank:058), so
    no fuzzy-matching is needed when the merchant taps one.

    Returns True if Meta accepted the request, False on failure.
    Rule 10: callers must handle False and send a plain-text fallback.
    """
    phone_number_id, access_token = _meta_credentials()
    if not phone_number_id or not access_token:
        log.error("whatsapp_credentials_not_set")
        return False

    banks = COMMON_BANKS_BY_COUNTRY.get(country, COMMON_BANKS_BY_COUNTRY["nigeria"])
    # Hard cap at 9 to leave room for "Other" and stay inside the 10-row limit.
    bank_rows = [
        {
            "id": encode_bank_id(b.code),
            "title": b.name[:24],  # Meta title limit
        }
        for b in banks[:9]
    ]
    # Always append "Other" as the final row.
    bank_rows.append({
        "id": _OTHER_ROW_ID,
        "title": _OTHER_ROW_TITLE[:24],
        "description": _OTHER_ROW_DESCRIPTION[:72],  # Meta description limit
    })

    country_label = country.capitalize()
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": (
                    f"Select your {country_label} bank or mobile money provider.\n\n"
                    "Can't find yours? Tap 'Other' at the bottom."
                )
            },
            "action": {
                "button": "Choose bank",
                "sections": [
                    {
                        "title": f"{country_label} Banks",
                        "rows": bank_rows,
                    }
                ],
            },
        },
    }

    try:
        META_API_VERSION = "v20.0"
        META_API_BASE = "https://graph.facebook.com"
        response = httpx.post(
            f"{META_API_BASE}/{META_API_VERSION}/{phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        log.info("onboard_bank_list_sent", to=to, country=country, rows=len(bank_rows))
        return True
    except Exception as exc:
        log.error("onboard_bank_list_send_failed", to=to, error=str(exc))
        return False
