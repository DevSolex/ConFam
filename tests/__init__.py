"""
ConFam test suite

Markers (defined in pyproject.toml):
  no_double_count    — idempotency / Rule 3 / Rule 11 tests
  ledger_append_only — Rule 2 structural enforcement tests
  integration        — tests requiring a live Postgres instance

CI job mapping (do not rename markers without updating ci.yml + CONTRIBUTING.md):
  no-double-count CI job      → pytest -m no_double_count
  ledger-append-only CI job   → pytest -m ledger_append_only
"""
