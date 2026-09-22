# Contributing to ConFam

Thank you for working on ConFam. This project moves money and builds a credit-data asset for merchants who have no other formal record of their business. A bug here is not a UX inconvenience — it is a lost payment, a broken ledger entry, or a misdirected payout. Please read this document before opening a pull request.

## The non-negotiables

All 11 engineering rules in [`docs/ENGINEERING_RULES.md`](./docs/ENGINEERING_RULES.md) are **strict and non-negotiable**. They are not aspirational guidelines. If a rule is blocking a legitimate need, the correct response is to open a PR that revises the rule in `docs/ENGINEERING_RULES.md` with the tradeoff stated in writing — not to create a silent exception in code.

The checklist below operationalises each rule as a concrete PR gate.

---

## PR checklist

Work through this list before requesting review. For each item, confirm it is either "yes, addressed" or "not applicable to this PR" (and state why).

### Rule 1 — Zero-custody enforced in code

- [ ] Does this PR introduce any code path that constructs a payout destination at request time?
  **If yes: stop. Payout destinations are read-only lookups against the pre-verified whitelist only.**
- [ ] Does this PR add any admin tool, support workflow, or configuration that could redirect a payout to an account that is not the merchant's own whitelisted account?
  **If yes: stop. That capability must not exist in code.**
- [ ] If this PR adds a new payment rail: has the zero-custody rule been verified to apply to the new rail without re-litigating it?

### Rule 2 — Ledger is append-only

- [ ] Does this PR touch `LedgerEntry` (the table or any ORM model / migration for it)?
  **If yes:** confirm that no `UPDATE` or `DELETE` path has been introduced on that table, even for "corrections." Corrections must be new rows with `entry_type = 'correction'` referencing the original via `corrects_entry_id`.
- [ ] Does this PR change the `PayoutAccount` table? If yes: confirm rows are only ever inserted, never updated or deleted. New accounts supersede old ones via the `superseded_by` chain.

### Rule 3 — Every confirmation path is idempotent

- [ ] Does this PR touch the settlement engine, webhook handlers, or ledger writes?
  **If yes:** does it include a test demonstrating that duplicate or out-of-order confirmation events for the same `rail_reference` produce exactly one `LedgerEntry`? (See Rule 11 — this is a hard gate, not a nice-to-have.)
- [ ] Has the `UNIQUE (rail, rail_reference)` constraint on `rail_events` been preserved? If a migration in this PR drops or weakens it, that is a Rule 3 violation.

### Rule 4 — Every transaction is traceable end-to-end

- [ ] Given a ledger row produced by code in this PR, can you follow the FK chain: `ledger_entry_id → rail_event_id → link_id → merchant_id → payout_account_id`?
  **If any link in that chain is broken or optional where the data model says it should be required, fix it before merging.**
- [ ] Are state transition timestamps recorded for any new state the PR introduces?

### Rule 5 — Payout address changes require re-verification and cooling-off

- [ ] Does this PR touch the payout account onboarding or update flow?
  **If yes:** confirm that: (a) an ownership check runs, (b) a cooling-off window is enforced before `active_from` is set, (c) the merchant is notified through an independent channel, and (d) the change is a new row, not an overwrite.

### Rule 6 — Monetary values are exact, never floating-point

- [ ] Are all monetary amounts stored and computed as `BIGINT` (kobo) in the database and as integers or exact-decimal types in application code?
  **Search the diff for `float`, `double`, `FLOAT`, `REAL`, `NUMERIC` without explicit precision, or any arithmetic that could produce a fractional kobo. Fix before merging.**
- [ ] If this PR involves a stablecoin-to-naira conversion: is the rate recorded alongside the transaction at settlement time, and is it a fixed-precision type?

### Rule 7 — Every rail must be reconciled against its own source of truth

- [ ] Does this PR add or change a payment rail integration?
  **If yes:** is there a corresponding reconciliation path (schedule + diff logic + mismatch-as-incident treatment)? A rail without reconciliation is not shippable.

### Rule 8 — Secrets are never in source

- [ ] Run `git diff --staged` and scan for anything that looks like a key, token, password, account number, or signing key. If any exist: **do not merge. Rotate the secret immediately if it was ever committed, even briefly.**
- [ ] Are all new credentials referenced via environment variables with corresponding entries in `.env.example` (using `PLACEHOLDER_` values only)?

### Rule 9 — Personal and financial data is protected by default

- [ ] Does this PR read or write merchant bank details, phone numbers, or transaction metadata?
  **If yes:** is the data encrypted at rest? Is access logged with the accessor's identity and a timestamp?
- [ ] Does this PR add any new logging? Confirm that no PII or financial details appear in log lines.

### Rule 10 — Every external integration has a documented failure policy

- [ ] Does this PR introduce or extend a call to WhatsApp Business API, the bank aggregator, or Stellar?
  **If yes:** answer these three questions in the PR description or in a `FAILURE_POLICY.md` within the relevant service directory:
  1. What does the system do if the integration is unreachable when the merchant tries to generate a link?
  2. What happens if a webhook never arrives?
  3. What does the buyer see, and what does the merchant see, in each failure mode?
  **An undocumented failure mode is a missing feature, not an edge case to handle later.**

### Rule 11 — No feature ships without proving it doesn't break the no-double-count guarantee

- [ ] Does this PR touch the settlement engine, the ledger, or webhook handling?
  **If yes:** it must include a test (extending the existing no-double-count test, not replacing it) that proves repeated, out-of-order, or duplicate confirmation events for the same transaction still produce exactly one `LedgerEntry`. This test must run in the `no-double-count` CI job category (see `.github/workflows/`). **PRs that touch settlement logic and lack this test will not be merged.**

---

## Additional checks

- **No [Proposed] items silently treated as decided.** If your implementation depends on a technology or design choice currently marked [Proposed] in `docs/TECH_STACK.md`, `docs/DATA_MODEL.md`, `docs/ARCHITECTURE.md`, or `docs/COMPLIANCE_SECURITY.md`, that item must be converted to [Decided] in the relevant doc, in this same PR, with the rationale stated.
- **`OPEN_QUESTIONS.md` updated.** If this PR resolves an open question, mark it resolved and note the decision. If it surfaces a new one, add it.
- **Steering files accurate.** If this PR changes a fundamental design decision, update the relevant `.kiro/steering/` file so every future session inherits the new constraint.

---

## How to propose a change to this checklist or to `ENGINEERING_RULES.md`

These rules are the grounding contract for the project. Revise them by opening a PR that changes `docs/ENGINEERING_RULES.md` directly, with the tradeoff stated in the PR description. A rule may not be overridden by a comment or by exception in a single unrelated PR.
