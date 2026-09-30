# services/messaging

**Responsibility:** The merchant-facing WhatsApp integration — receiving commands
from merchants and sending payment links, confirmations, and sales statements.

---

## What belongs here

- Receiving inbound messages from merchants via the Meta WhatsApp Cloud API.
- Parsing and routing WhatsApp commands (REGISTER, ONBOARD, PAY, LEDGER, UPDATE, CANCEL).
- Sending interactive messages (country buttons for REGISTER, bank list for ONBOARD).
- Generating payment links (via `confam.links.create_link`).
- Sending payment-confirmed notifications to merchants.
- Generating and delivering PDF sales statements (LEDGER command).

## What does NOT belong here

- Messaging buyers directly. ConFam never messages thread 1 (buyer-merchant).
  The merchant pastes the link manually.
- Any payment or payout logic. This layer is a messaging interface only — all
  financial operations go through `confam.paystack`, `confam.ledger`, etc.

---

## Stack

- **Language/framework:** Python / FastAPI
- **WhatsApp API:** Meta Cloud API (direct — not via Twilio BSP)
- **Logging:** `structlog`

---

## Command routing

```
REGISTER <name> [GHANA|NIGERIA]   → country buttons (if no suffix) or direct register
ONBOARD                           → interactive bank list message
ONBOARD <account> <bank name>     → typed onboard (fuzzy bank-name match)
PAY <amount> <description>        → create payment link
LEDGER                            → PDF sales statement
UPDATE <account> <bank name>      → payout account change (48h cooling-off)
CANCEL / STOP                     → cancel pending payout change
```

Interactive replies (from tapping buttons/list rows):
```
button_reply  register:<CC>:<name>  → complete REGISTER for that country
list_reply    bank:<code>           → store pending bank, ask for account number
list_reply    bank:other            → redirect to typed ONBOARD instructions
```

Pending bank selection state:
- Stored in `merchants.pending_bank_code` + `merchants.pending_bank_selected_at`
- TTL: 10 minutes (`PENDING_BANK_SELECTION_TTL_SECONDS` in `confam/interactive.py`)
- A bare 10-digit message within TTL completes onboarding; after TTL it is ignored

---

## Failure policy (Rule 10)

| Integration | Unreachable at link-creation time | Webhook never arrives |
|---|---|---|
| Meta Cloud API | Log error, link created successfully, merchant gets no reply — they can still share the link manually | N/A — outbound only |
| Paystack bank/resolve | Rate-limit or error reply sent to merchant; no payout account written | N/A |
| Paystack subaccount create | Error reply sent; merchant stays pending_verification | N/A |

---

## Status

**Implemented and live.**
