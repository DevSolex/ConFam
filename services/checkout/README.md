# checkout

**Responsibility:** The lightweight, unauthenticated web page generated per payment link. The buyer's only ConFam-facing interface.

See [`docs/ARCHITECTURE.md` §3.2](../../docs/ARCHITECTURE.md) for the authoritative description of this component.

## What belongs here

- Rendering the transaction details (item/amount, merchant name) so the buyer knows what they're paying for.
- Presenting exactly two payment options: bank transfer or Lobstr/Stellar.
- Real-time payment status polling or subscription (SSE/websocket) — this page is the buyer's only feedback loop, so confirmation must appear as soon as the rail confirms.
- Link expiry handling — displaying an appropriate message if the link has expired or already been paid.

## What does NOT belong here

- Any code that constructs, validates, or stores payment destinations.
- Any code that triggers or monitors the settlement/payout flow — that is owned entirely by `services/settlement-engine`.
- Any persistent state of its own — this page is stateless; all truth lives in the settlement engine.
- Buyer identity collection beyond what the chosen payment rail requires.

## Stack

- **Language/framework:** Python / FastAPI (OQ-002 resolved)
- **Real-time status:** Server-Sent Events (SSE) via FastAPI `StreamingResponse` (OQ-006 resolved)
- **Hosting:** `pay.confam.co/<link_id>` — generic domain, no per-merchant subdomain for the pilot (OQ-011 resolved)
- **Expiry:** 30 minutes from creation or first successful payment, whichever comes first (OQ-012 resolved)

## Status

**Scaffolded — not yet implemented.**

## Resolved decisions affecting this component

All open questions for this service are resolved. See `OPEN_QUESTIONS.md`.
