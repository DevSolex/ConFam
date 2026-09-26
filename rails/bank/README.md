# rails/bank

**Responsibility:** Integration adapter for the Nigerian bank/fiat payment rail.

See [`docs/ARCHITECTURE.md` §3.4](../../docs/ARCHITECTURE.md) and [`docs/TECH_STACK.md`](../../docs/TECH_STACK.md) (bank/fiat rail section) for context.

## Status

**Scaffolded — not yet implemented.**

## Placeholder note

The specific aggregator (Paystack, Flutterwave, or direct NIBSS) has **not been decided**. This directory is a placeholder so the repo structure is coherent before that decision is made.

See `OPEN_QUESTIONS.md` item: *Bank aggregator selection* — this must be resolved, and a commercial/compliance evaluation completed, before any code in this directory is written.

<!-- TODO: resolve OPEN_QUESTIONS.md "Bank aggregator choice" before implementing -->

## Key rules that apply

- Rule 3 — Idempotency: each bank webhook carries a unique `rail_reference`; processing the same reference twice must be a no-op.
- Rule 7 — Reconcile against the aggregator's own statement/API on a defined schedule.
- Rule 10 — Document the failure policy (unreachable aggregator, missing webhook) before this integration ships.
