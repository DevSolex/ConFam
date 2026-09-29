-- =============================================================================
-- Migration 015: add pending_bank_selection columns to merchants
-- =============================================================================
-- These two nullable columns support the tap-to-select ONBOARD flow:
-- when a merchant taps a bank from the interactive list, we store the resolved
-- bank code and the timestamp so the next bare account number message can
-- complete onboarding without fuzzy-matching.
--
-- Both columns are intentionally nullable:
--   NULL   = no pending selection (the normal state for all existing merchants)
--   non-NULL = merchant tapped a bank and we are waiting for their account number
--
-- The selection expires after PENDING_BANK_SELECTION_TTL_SECONDS (10 minutes,
-- defined as a named constant in confam/interactive.py). Expired rows are
-- treated as NULL at application layer and cleared on the next ONBOARD
-- interaction. No DB-level expiry job is needed for the pilot — the volume
-- is too low to warrant it.

ALTER TABLE merchants
    ADD COLUMN IF NOT EXISTS pending_bank_code TEXT;

ALTER TABLE merchants
    ADD COLUMN IF NOT EXISTS pending_bank_selected_at TIMESTAMPTZ;

COMMENT ON COLUMN merchants.pending_bank_code IS
    'Bank code selected via the interactive list message tap. '
    'NULL when no selection is pending. Cleared after onboarding completes '
    'or after PENDING_BANK_SELECTION_TTL_SECONDS (10 minutes).';

COMMENT ON COLUMN merchants.pending_bank_selected_at IS
    'Timestamp when the merchant tapped a bank in the interactive list. '
    'Used to enforce the 10-minute expiry on the pending selection. '
    'NULL when no selection is pending.';
