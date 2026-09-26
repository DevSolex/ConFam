# messaging

**Responsibility:** The ConFam-facing WhatsApp integration — the merchant's interface for generating payment links and receiving settlement notifications.

See [`docs/ARCHITECTURE.md` §3.1](../../docs/ARCHITECTURE.md) for the authoritative description of this component.

## What belongs here

- Receiving inbound messages from merchants in the ConFam thread (WhatsApp Business API webhook handler).
- Parsing merchant-supplied transaction details (item description, amount, buyer identifier).
- Dispatching payment link generation requests to `services/settlement-engine`.
- Delivering settlement notifications back to the merchant in the ConFam thread.

## What does NOT belong here

- Anything that talks to a buyer directly. ConFam never messages a buyer through any channel — the merchant relays the payment link manually. Code that attempts to initiate outbound messages to buyers must not exist in this service.
- Any payment or payout logic. This layer is strictly a messaging interface — it delegates to `services/settlement-engine` for all financial operations.

## Stack

- **Language/framework:** Python / FastAPI (OQ-002 resolved)
- **BSP:** Twilio (recommended) or 360dialog — pilot uses BSP, not direct Meta Cloud API (OQ-001 resolved)
- **Logging:** `structlog` (OQ-009 resolved)

## Status

**Scaffolded — not yet implemented.**

## Resolved decisions affecting this component

- OQ-001: Twilio BSP for the pilot. Account creation and WhatsApp Business verification must start now — it has lead time.
- OQ-010: ConFam has no footprint in thread 1 (buyer-merchant). Merchant brings their own number.
- OQ-002: Python / FastAPI.

Failure policy (Rule 10) for WhatsApp API unreachability must be documented here before this service ships.
