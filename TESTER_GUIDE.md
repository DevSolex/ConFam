# Tester Guide — ConFam WhatsApp + Checkout Flow

This guide walks a tester through the full merchant and buyer flows. It covers
both the WhatsApp command interface and the buyer-facing Paystack checkout.

## Prerequisites

- Paystack **test-mode** keys (`PAYSTACK_SECRET_KEY`). Never use live keys.
- A ConFam WhatsApp number (the Meta Cloud API number the merchant messages).
- Your WhatsApp number registered as a test recipient in Meta's App dashboard.
- `CHECKOUT_BASE_URL` set to `https://confam-xq4m.onrender.com/pay`.

---

## Merchant flow (WhatsApp commands)

All commands are sent to the ConFam WhatsApp number. Responses arrive
in the same thread within a few seconds.

### 1. Register

```
REGISTER <business name>
```

If no country suffix is given, ConFam replies with **two tappable buttons**:
Nigeria / Ghana. Tap the correct one — registration completes immediately.

To skip buttons and register directly with a typed suffix:
```
REGISTER Adaeze Fashion Store GHANA
REGISTER Adaeze Fashion Store NIGERIA
```

### 2. Onboard (set payout account)

**Option A — tap to select (recommended):**

Send bare `ONBOARD`. ConFam replies with a tappable list of up to 9 common
banks / mobile money providers + an "Other" row. Tap your bank. ConFam asks
for your account number. Reply with your 10-digit account number. Done.

**Option B — fully typed:**
```
ONBOARD <10-digit account number> <bank name>
```
Examples:
```
ONBOARD 0123456789 GTBank
ONBOARD 0123456789 Access Bank
ONBOARD 0241234567 MTN MoMo
```

Bank name is fuzzy-matched — "GTBank", "Guaranty Trust", "GT Bank PLC" all work.
If the name is ambiguous, ConFam replies with up to 3 candidate suggestions.

Account number format:
- Nigeria: 10-digit NUBAN
- Ghana banks (ghipps): 10-digit account number
- Ghana mobile money: 10-digit subscriber phone number (e.g. 0241234567 for MTN)

### 3. Create a payment link

```
PAY <amount> <description>
```
Example:
```
PAY 2500 Jordan — 1 pair sneakers
```
ConFam replies with a `pay.confam.co/<link_id>` URL. Paste it to your buyer.
Amount is in naira (NGN) for Nigeria merchants, cedis (GHS) for Ghana.

### 4. Get a sales statement

```
LEDGER
```
ConFam sends a PDF of your confirmed sales as a WhatsApp document attachment.

### 5. Update payout account

```
UPDATE <account number> <bank name>
```
A 48-hour cooling-off window applies. ConFam sends an immediate notification.
Reply `CANCEL` within 48 hours to abort the change.

---

## Buyer flow (checkout page)

1. Buyer opens the link pasted by the merchant.
2. Page shows the item, amount, and merchant name, plus an email field.
3. Buyer picks a payment method and is redirected to Paystack's hosted page.
4. Page polls every 3 seconds (max 5 min) and shows "Payment received ✅"
   once the Paystack webhook confirms.

**Paystack test card:**
```
Number: 4084 0840 8408 4081
Expiry: any future date   CVV: 408
```

---

## Verifying a payment in the DB

```sql
SELECT disposition, processed FROM rail_events  WHERE link_id = '<link>';
SELECT COUNT(*)               FROM ledger_entries WHERE link_id = '<link>';
SELECT status                 FROM payment_links  WHERE link_id = '<link>';
```

Expected after a clean payment: `disposition=applied`, `processed=true`,
`COUNT=1`, `status=logged`.

---

## Edge cases

| Scenario | Expected |
|---|---|
| Expired link | 410 — "This payment link has expired" |
| Already-paid link | Page shows paid state, no Pay button |
| Duplicate webhook (same reference) | 200, idempotent — no second ledger entry |
| Second charge on same link (different reference) | Rejected — link already logged |
| Unknown link | 404 |
| Bank name not found | Reply with up to 3 closest candidates |
| Bare `ONBOARD` | Interactive bank list message sent |
| `REGISTER` with no country | Country buttons sent (Nigeria / Ghana) |
| Account number after pending-bank TTL expires (>10 min) | Treated as normal text, not completing onboard |
| `CANCEL` with no pending change | Polite "nothing to cancel" reply |

---

## Admin endpoints (bypass WhatsApp — useful for direct settlement-engine tests)

```bash
# Create a merchant
curl -X POST https://confam-xq4m.onrender.com/merchants \
  -H "X-Admin-Key: <your-admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"whatsapp_number": "+234...", "business_name": "Test Store"}'

# Create a payment link
curl -X POST https://confam-xq4m.onrender.com/links \
  -H "X-Admin-Key: <your-admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"merchant_id": "...", "amount_minor_units": 75000, "currency": "NGN", "description": "Item"}'
```

---

## Notes

- Test mode only — Paystack test cards settle no real money.
- Do not point real merchants at the service while `PAYSTACK_SECRET_KEY` is a
  test key — a buyer could think they were charged.
- OPay appears inside Paystack's "bank" channel widget — it is not a separate
  top-level method in ConFam.
