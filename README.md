# ConFam

**ConFam gives informal chat-commerce merchants a balance sheet.**

> "We are not digitising them — we are giving them a balance sheet."

This repository (and this documentation set) exists to **ground** ConFam before a single line of production code is written. It is the shared source of truth for what ConFam is, what it is not, how its pieces fit together, and which rules are non-negotiable. Nothing here is the product itself — it is the contract the team is building against.

## 1. The problem

Merchants in Nigeria (and similar markets) sell almost entirely through chat apps — 67% of Nigerian online purchases start in a chat thread, and Nigeria is the #1 country globally for WhatsApp Business commerce. That commerce is invisible to the formal economy:

- **No real payments** — buyers send screenshots of bank transfers as "proof"; sellers manually reconcile against their banking app; fake screenshots slip through.
- **No records** — a $2B+ market runs with no structured trade history.
- **No credit** — with no sales record, merchants have nothing a lender can underwrite against.

## 2. The solution, in one paragraph

A merchant keeps selling exactly as they already do, in a normal WhatsApp conversation with a buyer. When it's time to collect payment, the merchant opens a **separate ConFam thread**, enters the transaction details, and receives a one-time payment link. That link — not a new app, not a new account for the buyer — is what gets shared back into the original buyer conversation. The buyer pays via bank transfer or a self-custody stablecoin wallet (Lobstr on Stellar). ConFam never custodies funds; it confirms the payment, pays the merchant's own whitelisted bank account, notifies the merchant, and writes an immutable row to a naira-denominated ledger. That ledger — not the payment itself — is the long-term asset: it becomes the underwriting file for working-capital lending later.

See [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for the full system breakdown.

## 3. Core, non-negotiable principles

These are elaborated with full rationale in [`docs/ENGINEERING_RULES.md`](./docs/ENGINEERING_RULES.md), but they are stated here because everything else in this repo is downstream of them:

1. **Zero-custody is enforced in code, not policy.** ConFam is never a valid resting place for a naira or a dollar. A payout can only ever land on a merchant's pre-verified, whitelisted account.
2. **The ledger is append-only and is the product.** Nothing that has been written to it is ever edited or deleted. It is the asset the entire business model (fees → subscription → credit) is built on top of.
3. **No behavior change for either side.** Merchants and buyers keep using WhatsApp exactly as they already do. ConFam inserts itself only at the moment of payment.
4. **Every transaction must be traceable end-to-end**, from the chat message that triggered it to the ledger row it produced.

## 4. Documentation map

| Doc | Purpose |
|---|---|
| [`README.md`](./README.md) | This file — orientation and non-negotiables |
| [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) | System components, the two-thread model, end-to-end flow |
| [`docs/TECH_STACK.md`](./docs/TECH_STACK.md) | Technologies, confirmed vs. proposed, and why |
| [`docs/ENGINEERING_RULES.md`](./docs/ENGINEERING_RULES.md) | Strict rules every contributor and every PR must follow |
| [`docs/DATA_MODEL.md`](./docs/DATA_MODEL.md) | Core entities, schema, and the transaction state machine |
| [`docs/COMPLIANCE_SECURITY.md`](./docs/COMPLIANCE_SECURITY.md) | Licensing, data protection, KYC/KYB, and messaging-policy constraints |
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | PR checklist derived from the 11 engineering rules |
| [`OPEN_QUESTIONS.md`](./OPEN_QUESTIONS.md) | Every [Proposed] item that must be decided before feature work begins |
| [`RUNNING.md`](./RUNNING.md) | How to start the stack, run tests, and manually smoke-test the payment link flow |

## 5. What "grounded" means here

This documentation set intentionally separates three categories of claim, and every doc in this set keeps that separation explicit:

- **Confirmed** — stated directly in ConFam's pitch materials or existing test suite (e.g., Postgres as the ledger store, Stellar + Lobstr as the crypto rail, 0.5–1% transaction fee).
- **Decided in grounding** — resolved during this documentation pass and now treated as binding unless revisited on purpose (e.g., the two-WhatsApp-thread model, link-based payment requests).
- **Proposed default** — a reasonable engineering choice made to unblock grounding, flagged as needing explicit team sign-off before implementation (e.g., specific backend language/framework, specific bank aggregator).

Anything marked **proposed default** should be treated as a placeholder, not a decision. Do not build against it without converting it to "decided" first.

## 6. Status

Pre-revenue. Per existing project traction: the core loop (WhatsApp → payment → auto-confirm → receipt → ledger) is built as a single codebase across two settlement rails, with 49 automated tests passing (including a live-Postgres test and a 50+ transaction no-double-count test) and ~2ms confirmation-to-ledger latency. This documentation set formalizes the rules that existing and future code must continue to satisfy.
