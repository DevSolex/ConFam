# db/seeds

Development and test seed data **only**. These files must never contain:

- Real merchant names, phone numbers, or bank details.
- Real or realistic-looking account numbers, BVNs, or API credentials.
- Monetary amounts that represent real transactions.

## Placeholder data guidelines

Use obviously fake identifiers:
- Phone numbers: `+234-000-TEST-001`, `+234-000-TEST-002`, etc.
- Bank account numbers: `0000000001`, `0000000002`, etc. (clearly not real NUBAN format)
- Amounts: round kobo values chosen for test readability (e.g. `10000` = ₦100.00)

## What seed data is for

- Standing up a local development environment quickly.
- Providing fixtures for the CI test suite (especially the no-double-count tests).
- Demonstrating the state machine transitions (see `docs/DATA_MODEL.md §2`) without requiring real rail confirmations.

## Seed files

<!-- Add seed files here as the project's test suite grows. -->
<!-- Example: 001_dev_merchants.sql — creates two test merchant rows -->
<!-- Example: 002_dev_payment_links.sql — creates links in each status -->
