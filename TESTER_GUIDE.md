# Tester Guide — Paystack Checkout

This guide walks a tester through the buyer-facing checkout flow and webhook payment handling. It is the manual companion to `tests/test_checkout_payment_ui.py` and the automated suite.

## Prerequisites

- Paystack **test-mode** keys (`PAYSTACK_SECRET_KEY`, and a `PAYSTACK_PUBLIC_KEY` only if you add client-side SDK work). Never use live keys.
- A whitelisted payout account on the merchant whose link you are testing (`merchant_payout_accounts.paystack_subaccount_code`). Checkout requires a subaccount code; without one the page returns 503 and the link is never sent to Paystack.
- `CHECKOUT_BASE_URL` set to the public root (e.g. `https://confam.onrender.com/pay`) so Paystack can route the buyer back after payment. It must end in `/pay` when served by the unified app.

## What the flow is

1. Admin creates a payment link for a merchant: `POST /links` with `X-Admin-Key` → returns `checkout_url` + `link_id`.
2. Buyer opens `GET /pay/{link_id}` and sees the item, amount, merchant name, and an email field for the receipt.
3. Buyer picks a method and is redirected to Paystack's hosted page:
   - card → `channels: ["card"]`
   - bank transfer → `channels: ["bank_transfer"]`
   - bank (incl. OPay) → `channels: ["bank"]`  *(OPay is a widget option inside Paystack's "bank" channel, not a separate channel)*
   - USSD → `channels: ["ussd"]`
4. The page polls `GET /pay/{link_id}/status` every 3 seconds (max 5 minutes) and shows confirmation ("Payment received") when the webhook flips the link to `logged`.
5. The Paystack webhook (`POST /webhooks/paystack`, HMAC-verified) is the only thing that moves money state. The browser callback is decorative — ConFam never trusts the `reference`/`trxref` query params.

## Manual test script

### Happy path (per method)

| Method | Expected |
|---|---|
| card | Redirect to Paystack hosted page → complete a test card → webhook → page shows "Payment received", link `logged` |
| bank_transfer | Redirect → Paystack shows bank details → buyer pays → webhook → confirmation |
| bank / OPay | Redirect → Paystack bank widget (OPay listed) → pay → webhook → confirmation |
| ussd | Redirect → Paystack shows USSD code → pay → webhook → confirmation |

For each: after redirect back, verify the sale is recorded exactly once:
`SELECT * FROM ledger_entries WHERE link_id = '<link>'` (one row), and the link status is `logged`.

### Error / edge cases

- **Expired link:** open a link past `PAYMENT_LINK_EXPIRY_SECONDS` → 410 page, "This payment link has expired".
- **Already-paid link:** open a `logged` link → page shows paid state, no "Pay" button.
- **No payout account / no subaccount:** create a link for a merchant with no active payout account → `POST /pay/{link_id}/pay` returns 503 with a clear message; Paystack is never called.
- **Bad method:** `POST /pay/{link_id}/pay` with `method: "opay"` → 422 (OPay is not a top-level method; use `bank`).
- **Bad email:** missing or invalid receipt email → 422.
- **Unknown link:** `GET /pay/nonexistent` → 404 page.
- **Duplicate charge (the important one):** pay once (link → `logged`), then send a *second, different-reference* `charge.success` webhook for the same link. Expect: no second sale, link stays `logged`, log + Sentry error saying a manual refund is required. There must be exactly one line in `ledger_entries`.
- **Retried webhook (same reference):** resend the exact same `charge.success` → returns 200, does nothing (UNIQUE constraint on `rail_reference`), no Sentry alert.
- **Late payment:** let the link expire, then send a webhook anyway → the sale is still recorded (marked `applied_after_expiry`), link becomes `logged`, warning log only, no Sentry.

## Where to look in the DB

```sql
-- was a sale written, exactly once, with the right disposition?
SELECT disposition, processed FROM rail_events WHERE link_id = '<link>';
SELECT COUNT(*) FROM ledger_entries WHERE link_id = '<link>';
SELECT status FROM payment_links WHERE link_id = '<link>';
```

Dispositions: `applied` (normal), `applied_after_expiry` (late), `duplicate` (second charge — needs manual refund).

## Caveats

- **Test mode ≠ live.** Paystack test cards settle no real money; OPay/USSD behavior in test mode must be confirmed on Paystack's dashboard before trusting it in a demo.
- **Don't point real merchants at the checkout URL in test mode** — a buyer could think they were charged.
- Verify subaccount routing in the Paystack dashboard (the `subaccount_code` in the log must match the payout account).