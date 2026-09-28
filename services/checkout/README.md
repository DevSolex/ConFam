# checkout

**Responsibility:** The lightweight, unauthenticated web page generated per payment link. The buyer's only ConFam-facing interface.

See [`docs/ARCHITECTURE.md` §3.2](../../docs/ARCHITECTURE.md) for the authoritative description of this component.

## What belongs here

- Rendering the transaction details (item/amount, merchant name) so the buyer knows what they're paying for.
- Presenting the payment options via Paystack's hosted page: card, bank transfer, bank (incl. OPay), or USSD. Card details never touch ConFam (PCI-DSS SAQ-A).
- Real-time payment status polling — this page is the buyer's only feedback loop, so confirmation must appear as soon as the rail confirms.
- Link expiry handling — displaying an appropriate message if the link has expired or already been paid.

## What does NOT belong here

- Any code that constructs, validates, or stores payment destinations.
- Any code that triggers or monitors the settlement/payout flow — that is owned entirely by `services/settlement-engine`.
- Any persistent state of its own — this page is stateless; all truth lives in the settlement engine.
- Buyer identity collection beyond what the chosen payment rail requires.

## Stack

- **Language/framework:** Python / FastAPI (OQ-002 resolved)
- **Real-time status:** buyer-side polling of `GET /{link_id}/status` every 3s, capped at 5 minutes (OQ-006 resolved — polling replaces SSE)
- **Hosting:** `pay.confam.co/<link_id>` — generic domain, no per-merchant subdomain for the pilot (OQ-011 resolved)
- **Expiry:** 30 minutes from creation or first successful payment, whichever comes first (OQ-012 resolved)

## Status

**Implemented.** Buyer-facing Paystack checkout page with webhook-driven confirmation polling.

## Resolved decisions affecting this component

All open questions for this service are resolved. See `OPEN_QUESTIONS.md`.
