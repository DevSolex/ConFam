# rails/stellar

**Responsibility:** Integration adapter for the Stellar/Lobstr crypto rail.

See [`docs/ARCHITECTURE.md` §3.4](../../docs/ARCHITECTURE.md) and [`docs/TECH_STACK.md`](../../docs/TECH_STACK.md) (crypto/stablecoin rail section) for context.

## Status

**Scaffolded — not yet implemented.**

## What belongs here

- Listening for Stellar ledger events confirming a buyer payment to the designated address.
- Normalising the Stellar event payload into the `RailEvent` format (see `docs/DATA_MODEL.md`).
- The `rail_reference` for this rail is the Stellar transaction hash — used as the idempotency key.

## What does NOT belong here

- Fiat off-ramp (stablecoin → naira) logic — that is an anchor-protocol interaction owned by `services/settlement-engine`.
- Currency conversion arithmetic — Rule 6 requires the rate to be computed exactly once at settlement time and recorded alongside the transaction.

## Placeholder note

The exact Stellar SEP variant (SEP-24 interactive vs. SEP-31 direct) for stablecoin-to-naira conversion has **not been decided**. This depends on which anchor is used.

See `OPEN_QUESTIONS.md` item: *Stellar anchor selection and SEP variant*.

<!-- TODO: resolve OPEN_QUESTIONS.md "Stellar anchor / SEP variant" before implementing -->

## Key rules that apply

- Rule 3 — Idempotency on Stellar transaction hash.
- Rule 6 — Conversion rate recorded once at settlement; never recomputed.
- Rule 7 — Reconcile against Stellar Horizon on a defined schedule.
- Rule 10 — Document the failure policy for Stellar network unavailability.
