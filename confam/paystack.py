"""
confam.paystack — Paystack API client and webhook verification.

Responsibilities:
  - initialize_transaction(): call Paystack's Initialize Transaction API,
    passing the merchant's subaccount code from the whitelisted PayoutAccount.
  - verify_webhook_signature(): HMAC-SHA512 verification of inbound webhooks.

What this module does NOT do:
  - It does not decide where money goes (that's the whitelist lookup in
    confam.payout_accounts, which happens before this module is called).
  - It does not write to the database.
  - It does not contain any business logic beyond making the API call and
    verifying signatures.

Engineering Rule 1: the subaccount_code passed to initialize_transaction()
must come from a confam.payout_accounts.PayoutAccount read from the whitelist,
never from user input or runtime construction.

Engineering Rule 8: PAYSTACK_SECRET_KEY is read from environment, never logged.

Paystack Split Payment model (verified against docs.paystack.com):
  - The buyer pays the full amount on Paystack's hosted checkout page.
  - Paystack's settlement engine splits the funds: the merchant's share goes
    to their bank account per their settlement_schedule (default: AUTO = next
    business day), ConFam's platform share stays in ConFam's Paystack balance.
  - ConFam never initiates a separate transfer and never holds buyer funds in
    transit — the split routing is inside Paystack's own settlement engine.
  - The charge.success webhook fires when the buyer's payment is collected,
    NOT when the merchant's bank account is credited (settlement happens later
    on Paystack's schedule). The ledger entry records "payment confirmed by
    Paystack," not "merchant's bank account credited."
"""

import difflib
import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

PAYSTACK_API_BASE = os.environ.get("PAYSTACK_BASE_URL", "https://api.paystack.co")

# Paystack's own channel names, accepted in the `channels` array of Initialize
# Transaction / Create Charge. Source: Paystack Transaction API reference.
#
# We offer a subset to buyers (see services/checkout/pay.py METHOD_CHANNELS).
# The rest are listed here so a typo is caught before the API call rather than
# silently ignored by Paystack, which falls back to showing every enabled
# channel. "bank" is Pay-with-Bank, which is also where OPay appears as an
# option — OPay is not a separate channel.
PAYSTACK_CHANNELS = frozenset({
    "card",
    "bank",
    "bank_transfer",
    "ussd",
    "qr",
    "eft",
    "mobile_money",
})


class PaystackError(Exception):
    """Raised when the Paystack API returns an error or is unreachable."""


class PaystackRateLimited(PaystackError):
    """Raised when Paystack responds 429 (API quota exhausted).

    Distinguished from a generic PaystackError so callers can tell "this account
    is wrong" (merchant must fix their input) apart from "we are being
    throttled" (merchant should simply retry later, and the error is ours, not
    theirs). Subclassing PaystackError keeps existing `except PaystackError`
    handlers working.
    """


class WebhookSignatureInvalid(Exception):
    """
    Raised when the x-paystack-signature header does not match the payload.
    An unverified webhook must never be processed (Rule 1's security boundary).
    """


@dataclass(frozen=True)
class InitializedTransaction:
    """Result of a successful Initialize Transaction API call."""
    authorization_url: str   # Redirect the buyer here
    access_code: str
    reference: str           # Paystack's transaction reference — used as rail_reference


@dataclass(frozen=True)
class ResolvedAccount:
    """Result of a successful bank/resolve API call."""
    account_number: str
    account_name: str    # Bank-verified account holder name
    bank_id: int


@dataclass(frozen=True)
class CreatedSubaccount:
    """Result of a successful Create Subaccount API call."""
    subaccount_code: str   # e.g. ACCT_xxxxxxxxxx — stored on PayoutAccount
    business_name: str


@dataclass(frozen=True)
class BankListEntry:
    """One entry from Paystack's GET /bank list."""
    name: str
    code: str
    country: str


class BankNameNotResolved(PaystackError):
    """Raised when a typed bank name cannot be confidently matched to a Paystack bank code.

    `candidates` carries the top-3 closest banks from the list so the caller
    can show suggestions to the merchant rather than just saying "not found".
    """
    def __init__(self, message: str, candidates: list[BankListEntry]) -> None:
        self.candidates = candidates
        super().__init__(message)


# In-memory cache: country -> (fetched_at_monotonic, list[BankListEntry])
# A one-hour TTL is adequate — the bank list is stable and the list is small.
_BANK_LIST_CACHE: dict[str, tuple[float, list[BankListEntry]]] = {}
_BANK_LIST_CACHE_TTL_SECONDS = 3600

# Noise words to strip before fuzzy matching a bank name.
# These appear in almost every bank name and add no discriminating signal.
_BANK_NOISE_WORDS = frozenset({
    "bank", "plc", "ltd", "limited", "microfinance", "mfb",
    "savings", "finance", "nigeria", "ghana", "the",
})


def _secret_key() -> str:
    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    if not key:
        raise PaystackError(
            "PAYSTACK_SECRET_KEY is not set. "
            "Set it in .env (test mode) or AWS Secrets Manager (production). "
            "Engineering Rule 8: never hardcode this value."
        )
    return key


def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> None:
    """
    Verify the x-paystack-signature HMAC-SHA512 header.

    Paystack signs the raw request body with your secret key using HMAC-SHA512.
    This must be called before trusting anything in the webhook payload.

    Raises WebhookSignatureInvalid if the signature does not match.
    Raises PaystackError if PAYSTACK_SECRET_KEY is not set.

    This is not optional hardening — it is the entire security boundary of
    the webhook endpoint. A fabricated "charge.success" event from a bad actor
    would cause ConFam to write a fraudulent LedgerEntry.
    """
    expected = hmac.new(
        _secret_key().encode("utf-8"),
        payload_bytes,
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        log.warning("paystack_webhook_signature_invalid")
        raise WebhookSignatureInvalid(
            "x-paystack-signature header does not match payload. "
            "Request rejected."
        )


def initialize_transaction(
    *,
    amount_minor_units: int,
    email: str,
    subaccount_code: str,
    reference: str,
    link_id: str,
    transaction_charge_minor_units: int = 0,
    channels: list[str] | None = None,
    callback_url: str | None = None,
) -> InitializedTransaction:
    """
    Call Paystack's Initialize Transaction API and return the authorization URL.

    Parameters:
        amount_minor_units: total amount in kobo (Rule 6 — always int).
        email: buyer's email address (required by Paystack).
        subaccount_code: the merchant's Paystack subaccount code, read from
            the whitelisted PayoutAccount (Rule 1 — never constructed at runtime).
        reference: a unique reference for this transaction (we use the link_id).
        link_id: embedded in metadata so the webhook handler can match the
            Paystack reference back to the ConFam PaymentLink.
        transaction_charge_minor_units: ConFam's platform fee in kobo (flat fee
            that stays in ConFam's Paystack balance). The remainder goes to the
            merchant's subaccount. 0 means full amount goes to the subaccount.
        channels: restricts what the buyer may pay with, e.g. ["card"] or
            ["bank_transfer"]. Omit to let Paystack show everything the account
            has enabled. Valid values are Paystack's own channel names — see
            PAYSTACK_CHANNELS below. Note these are *channel* names, not our
            buyer-facing method names; services/checkout/pay.py owns that map.
        callback_url: fully qualified URL Paystack redirects the buyer to after
            payment. Omit to use the URL configured on the Paystack dashboard.
            We always set it so the buyer lands back on this link's page.

    Raises PaystackError on API failure or unreachability.

    NOTE on settlement timing: charge.success fires when the buyer's payment
    is collected. The merchant's bank account is credited later, per their
    settlement_schedule (default AUTO = next business day). The LedgerEntry
    written on charge.success records payment confirmation, not bank credit.

    NOTE on the subaccount split: `subaccount` and `channels` are independent
    parameters and Paystack applies the split when the charge settles, so the
    merchant's share is routed without ConFam ever holding the money. This holds
    for every channel we offer, but Paystack's docs never state it explicitly
    per channel — see the verification notes in docs/PAYSTACK_VERIFICATION.md
    for what is confirmed, what is inferred, and what still needs checking in
    test mode by reading the split on a real transaction.
    """
    if not isinstance(amount_minor_units, int) or isinstance(amount_minor_units, bool):
        raise TypeError(
            f"amount_minor_units must be int (kobo), got {type(amount_minor_units).__name__}. "
            "Engineering Rule 6."
        )

    if channels is not None:
        if not channels:
            raise ValueError("channels must contain at least one channel or be omitted")
        unknown = [c for c in channels if c not in PAYSTACK_CHANNELS]
        if unknown:
            # Fail before the API call: Paystack silently ignores unrecognised
            # channel names and falls back to showing everything, which would
            # quietly widen what the buyer can pay with.
            raise ValueError(
                f"Unknown Paystack channel(s): {unknown}. "
                f"Valid channels: {', '.join(sorted(PAYSTACK_CHANNELS))}."
            )

    payload: dict = {
        "amount": amount_minor_units,
        "email": email,
        "reference": reference,
        "subaccount": subaccount_code,
        "metadata": {
            "confam_link_id": link_id,
            "cancel_action": "payment_cancelled",
        },
    }
    if channels is not None:
        payload["channels"] = list(channels)
    if callback_url:
        payload["callback_url"] = callback_url
    if transaction_charge_minor_units > 0:
        payload["transaction_charge"] = transaction_charge_minor_units

    try:
        response = httpx.post(
            f"{PAYSTACK_API_BASE}/transaction/initialize",
            headers={
                "Authorization": f"Bearer {_secret_key()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException as exc:
        raise PaystackError(
            f"Paystack Initialize Transaction timed out for link {link_id}. "
            "Check Rule 10 failure policy in rails/bank/README.md."
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise PaystackError(
            f"Paystack Initialize Transaction failed: {exc.response.status_code} "
            f"{exc.response.text[:200]}"
        ) from exc

    if not data.get("status"):
        raise PaystackError(
            f"Paystack Initialize Transaction returned status=false: {data.get('message')}"
        )

    tx = data["data"]
    return InitializedTransaction(
        authorization_url=tx["authorization_url"],
        access_code=tx["access_code"],
        reference=tx["reference"],
    )


def resolve_bank_account(
    *,
    account_number: str,
    bank_code: str,
) -> ResolvedAccount:
    """
    Call Paystack's GET /bank/resolve to verify a bank account exists and
    retrieve the account holder's name.

    Used during merchant payout account onboarding to confirm the submitted
    bank details are valid before creating a subaccount.

    Raises PaystackError if the account cannot be resolved (invalid details,
    API error, or unreachable).

    Rule 9: the returned account_name is stored for audit purposes. It is not
    automatically matched against the merchant's claimed business name at this
    verification tier — that is a future KYB-depth feature.

    Raises PaystackRateLimited if Paystack throttles us (429).
    Raises PaystackError if the account cannot be resolved.

    PRIVACY: the exception messages below deliberately exclude the submitted
    account number and the raw response body. Paystack echoes the account
    number back in some error payloads, and these strings reach both structured
    logs and merchant-facing chat replies, so only the status code is carried.
    """
    try:
        response = httpx.get(
            f"{PAYSTACK_API_BASE}/bank/resolve",
            headers={"Authorization": f"Bearer {_secret_key()}"},
            params={"account_number": account_number, "bank_code": bank_code},
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException as exc:
        raise PaystackError("Paystack bank/resolve timed out.") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 429:
            raise PaystackRateLimited("Paystack bank/resolve rate limited (429).") from exc
        # 422 from Paystack means the account number / bank code combination is invalid.
        raise PaystackError(f"Paystack bank/resolve failed: HTTP {status}") from exc

    if not data.get("status"):
        raise PaystackError(
            f"Paystack bank/resolve returned status=false: {data.get('message')}"
        )

    acct = data["data"]
    return ResolvedAccount(
        account_number=acct["account_number"],
        account_name=acct["account_name"],
        bank_id=acct.get("bank_id", 0),
    )


def create_subaccount(
    *,
    business_name: str,
    bank_code: str,
    account_number: str,
    percentage_charge: float = 0.0,
) -> CreatedSubaccount:
    """
    Call Paystack's POST /subaccount to create a subaccount for a merchant.

    The subaccount_code returned is stored on the merchant's PayoutAccount row
    and used for all future split-payment initializations (Rule 1).

    percentage_charge: the share of each transaction that goes to ConFam's
    main account (e.g. 1.0 = 1%). The remainder goes to the merchant's
    subaccount. Currently 0 (no platform fee taken at this stage).

    Raises PaystackRateLimited if Paystack throttles us (429).
    Raises PaystackError on API failure.

    PRIVACY: as with bank/resolve, the exception message excludes the account
    number and the raw response body — see resolve_bank_account.
    """
    try:
        response = httpx.post(
            f"{PAYSTACK_API_BASE}/subaccount",
            headers={
                "Authorization": f"Bearer {_secret_key()}",
                "Content-Type": "application/json",
            },
            json={
                "business_name": business_name,
                "bank_code": bank_code,
                "account_number": account_number,
                "percentage_charge": percentage_charge,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException as exc:
        raise PaystackError("Paystack create subaccount timed out.") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 429:
            raise PaystackRateLimited("Paystack create subaccount rate limited (429).") from exc
        raise PaystackError(f"Paystack create subaccount failed: HTTP {status}") from exc

    if not data.get("status"):
        raise PaystackError(
            f"Paystack create subaccount returned status=false: {data.get('message')}"
        )

    sub = data["data"]
    return CreatedSubaccount(
        subaccount_code=sub["subaccount_code"],
        business_name=sub["business_name"],
    )


# ---------------------------------------------------------------------------
# Bank list — cached lookup and name resolution
# ---------------------------------------------------------------------------

def list_banks(country: str) -> list[BankListEntry]:
    """
    Return Paystack's bank list for `country`, using an in-memory cache.

    Paystack's GET /bank?country=<country> list rarely changes (new banks appear
    quarterly at most). We cache it for _BANK_LIST_CACHE_TTL_SECONDS (1 hour)
    so ONBOARD / UPDATE don't make an API call on every invocation.

    `country` must be the Paystack-recognised string — 'nigeria' or 'ghana'.

    Raises PaystackError on API failure or if the response carries status=false.
    Raises PaystackRateLimited on HTTP 429.

    The cache is module-level (process-local). In a multi-instance deployment
    each process fills its own cache independently, which is acceptable — the
    list is idempotent and the extra API calls are harmless. A Redis-backed
    cache is a future upgrade if the pilot grows to many concurrent instances.
    """
    now = time.monotonic()
    cached = _BANK_LIST_CACHE.get(country)
    if cached is not None:
        fetched_at, entries = cached
        if now - fetched_at < _BANK_LIST_CACHE_TTL_SECONDS:
            return entries

    try:
        response = httpx.get(
            f"{PAYSTACK_API_BASE}/bank",
            headers={"Authorization": f"Bearer {_secret_key()}"},
            params={"country": country, "perPage": "200", "use_cursor": "false"},
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException as exc:
        raise PaystackError(f"Paystack /bank list timed out for country={country}.") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 429:
            raise PaystackRateLimited(
                f"Paystack /bank list rate limited (429) for country={country}."
            ) from exc
        raise PaystackError(
            f"Paystack /bank list failed: HTTP {status} for country={country}"
        ) from exc

    if not data.get("status"):
        raise PaystackError(
            f"Paystack /bank list returned status=false for country={country}: "
            f"{data.get('message')}"
        )

    entries = [
        BankListEntry(
            name=b.get("name", ""),
            code=b.get("code", ""),
            country=b.get("country", country),
        )
        for b in data.get("data", [])
        if b.get("active", True) and b.get("code")
    ]

    _BANK_LIST_CACHE[country] = (time.monotonic(), entries)
    log.info("bank_list_fetched", country=country, count=len(entries))
    return entries


def _normalise_bank_name(name: str) -> str:
    """Lowercase, strip noise words, strip punctuation, collapse whitespace.

    'Guaranty Trust Bank PLC' -> 'guaranty trust'
    'GTBank'                  -> 'gtbank'
    'First Bank of Nigeria'   -> 'first'
    'Access Bank'             -> 'access'
    """
    # Lowercase and replace punctuation/hyphens with spaces
    cleaned = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    # Split and filter noise words; also filter single-char words and "of", "for"
    words = [
        w for w in cleaned.split()
        if w and w not in _BANK_NOISE_WORDS and w not in {"of", "for", "and"}
    ]
    return " ".join(words)


def _initialism(name: str) -> str:
    """Return the initialism of normalised meaningful words.

    'guaranty trust' -> 'gt'
    'access'         -> 'access'  (single word — return as-is, no abbreviation benefit)
    'united bank africa' -> 'uba'
    """
    words = _normalise_bank_name(name).split()
    if len(words) <= 1:
        return _normalise_bank_name(name)
    return "".join(w[0] for w in words if w)


def resolve_bank_name(bank_name_input: str, country: str) -> tuple[str, str]:
    """
    Fuzzy-match a merchant's typed bank name against Paystack's bank list and
    return (bank_code, canonical_bank_name).

    Matching strategy (confidence thresholds):
      1. Exact match after normalisation.
      2. Normalised needle is a substring of normalised candidate (or vice versa),
         and the overlapping part is at least 4 characters long.
      3. If no confident match is found via (1)/(2), falls back to difflib
         SequenceMatcher and uses the best ratio match if ratio >= 0.72.

    If no match is found at all, raises BankNameNotResolved with the top-3
    candidates by SequenceMatcher ratio so the caller can show suggestions.

    Raises PaystackError if list_banks() fails (API unreachable etc.).
    """
    banks = list_banks(country)
    needle = _normalise_bank_name(bank_name_input)
    # Also compute the "raw" needle with no noise stripping, for initialism matching
    needle_raw = re.sub(r"[^a-z0-9]", "", bank_name_input.lower())

    if not needle and not needle_raw:
        candidates = banks[:3]
        raise BankNameNotResolved(
            f"Could not parse bank name '{bank_name_input}'. "
            f"Found {len(candidates)} candidate(s).",
            candidates=candidates,
        )

    # --- Pass 1: exact and substring matches on normalised names ---
    for bank in banks:
        candidate = _normalise_bank_name(bank.name)
        if not candidate:
            continue
        # Exact normalised match
        if needle == candidate:
            return bank.code, bank.name
        # Substring (at least 4 chars overlap)
        overlap = needle if needle in candidate else (candidate if candidate in needle else "")
        if len(overlap) >= 4:
            return bank.code, bank.name

    # --- Pass 2: initialism match ---
    # e.g. input "GTBank" -> raw "gtbank"; candidate "Guaranty Trust Bank" -> initialism "gt"
    # We check if needle_raw starts with the bank's initialism (handles "gtbank", "uba", etc.)
    for bank in banks:
        bank_initialism = _initialism(bank.name)
        if len(bank_initialism) >= 2 and needle_raw.startswith(bank_initialism):
            return bank.code, bank.name
        # Also: if the needle IS the initialism exactly
        if needle_raw == bank_initialism and len(bank_initialism) >= 2:
            return bank.code, bank.name

    # --- Pass 3: fuzzy ratio fallback (difflib) ---
    scored: list[tuple[float, BankListEntry]] = []
    for bank in banks:
        candidate = _normalise_bank_name(bank.name)
        ratio = difflib.SequenceMatcher(None, needle, candidate).ratio()
        # Also score against raw needle vs raw bank name (catches typos in abbreviations)
        ratio_raw = difflib.SequenceMatcher(
            None, needle_raw, re.sub(r"[^a-z0-9]", "", bank.name.lower())
        ).ratio()
        scored.append((max(ratio, ratio_raw), bank))

    scored.sort(key=lambda t: t[0], reverse=True)
    top = [b for _, b in scored[:3]]

    best_ratio, best_bank = scored[0] if scored else (0.0, None)
    if best_ratio >= 0.72 and best_bank is not None:
        return best_bank.code, best_bank.name

    raise BankNameNotResolved(
        f"Could not confidently match '{bank_name_input}' to a bank in {country}. "
        f"Top candidates: {', '.join(b.name for b in top)}.",
        candidates=top,
    )
