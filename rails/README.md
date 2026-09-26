# rails

**Responsibility:** Integration adapters for each payment rail — bank/fiat and Stellar/crypto. Each rail lives in its own subdirectory and exposes a uniform interface to `services/settlement-engine`.

See [`docs/ARCHITECTURE.md` §3.4](../../docs/ARCHITECTURE.md) for the authoritative description of the payment rails.

## Subdirectories

```
rails/
  bank/       — Nigerian bank aggregator integration (Paystack / Flutterwave / NIBSS — see OPEN_QUESTIONS.md)
  stellar/    — Stellar network integration (Lobstr buyer wallet, anchor protocol for stablecoin-to-naira)
```

## What belongs here

- Webhook/event receivers that accept inbound confirmation signals from each rail.
- Normalisation of rail-specific confirmation payloads into the `RailEvent` format defined in `docs/DATA_MODEL.md`.
- **Nothing else.** Determining whether to write a `LedgerEntry` is the settlement engine's responsibility, not a rail adapter's.

## What does NOT belong here

- Any payout initiation logic — that belongs in `services/settlement-engine` and may only write to pre-whitelisted merchant accounts.
- Any business logic (idempotency checks, ledger writes, merchant notification) — those all live in `services/settlement-engine`.

## Status

**Scaffolded — not yet implemented.**

## Open questions that affect this component

See `OPEN_QUESTIONS.md`:
- Bank aggregator choice: Paystack, Flutterwave, or direct NIBSS? Currently [Proposed] in `docs/TECH_STACK.md`.
- Exact Stellar SEP variant (SEP-24 vs. SEP-31) for stablecoin-to-naira conversion? Currently [Proposed] in `docs/TECH_STACK.md`.
- Failure policy when a rail webhook never arrives — what is the reconciliation trigger cadence? (Engineering Rule 7 requires this to be defined.)
