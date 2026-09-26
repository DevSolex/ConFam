# settlement-engine

**Responsibility:** The core backend service that owns the entire payment confirmation and payout lifecycle.

See [`docs/ARCHITECTURE.md` §3.3](../../docs/ARCHITECTURE.md) for the authoritative description of this component.

## What belongs here

- Generating and signing one-time payment links.
- Receiving and processing webhook/event confirmations from both payment rails (bank and Stellar).
- Enforcing the payout whitelist (Engineering Rule 1 — zero-custody: payout destinations are **read-only lookups**, never constructed at request time).
- Writing confirmed transactions to the append-only ledger (Engineering Rule 2).
- Triggering merchant notification (decoupled from buyer-facing checkout confirmation).
- Idempotency enforcement — duplicate or out-of-order rail events are a guaranteed no-op (Engineering Rule 3).
- Reconciliation against each rail's own source of truth on a defined schedule (Engineering Rule 7).

## What does NOT belong here

- Any code that constructs or modifies a payout destination at runtime.
- Any code that contacts WhatsApp directly on behalf of a buyer.
- Business feature logic that has not yet been scoped in a documented, reviewed task.

## Stack

- **Language/framework:** Python / FastAPI (OQ-002 resolved)
- **Queue:** AWS SQS worker for inbound webhook processing (OQ-008 resolved)
- **Logging:** `structlog` (OQ-009 resolved)
- **Error tracking:** Sentry (OQ-009 resolved)
- **Reconciliation schedule:** every 15 minutes against Paystack statement API and Stellar Horizon (OQ-013 resolved)

## Python package

The Python source code lives in `services/settlement_engine/` (underscore) — Python cannot import from a hyphenated directory name. This `services/settlement-engine/` directory (hyphen) holds the README and documentation only.

The entry point is `services/settlement_engine/main.py`.

**Scaffolded — not yet implemented.**

The directory structure, configuration, and test categories exist to give feature work a clear home. No settlement, payout, or ledger logic has been written yet. All open questions that blocked implementation are resolved — see `OPEN_QUESTIONS.md` for the full resolution record. The one residual item is the Stellar anchor vendor (OQ-005 residual).

## Key engineering rules that apply most directly here

- Rule 1 — Zero-custody enforced in code.
- Rule 2 — Ledger is append-only.
- Rule 3 — Every confirmation path is idempotent.
- Rule 4 — Every transaction is traceable end-to-end.
- Rule 6 — Monetary values are exact integers (kobo), never floats.
- Rule 7 — Reconciliation against rail's own source of truth.
- Rule 10 — Every external integration has a documented failure policy.
- Rule 11 — Every PR touching this service must prove the no-double-count guarantee still holds.
