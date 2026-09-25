-- =============================================================================
-- Migration: 010_payout_account_change_flow
--
-- Prepares the payout_accounts table for the cooling-off change flow (OQ-021).
--
-- The existing UNIQUE INDEX payout_accounts_one_active_per_merchant
-- (merchant_id) WHERE superseded_by IS NULL AND active_from IS NOT NULL
-- was designed for a world where at most one unsuperseded row exists per
-- merchant at any time. The change flow requires TWO unsuperseded rows to
-- coexist temporarily:
--
--   1. The old active row:    active_from <= now(), superseded_by IS NULL
--   2. The new pending row:   active_from > now(),  superseded_by IS NULL
--
-- The index blocks the second INSERT because both rows satisfy the predicate.
-- This migration drops the index and replaces it with application-layer
-- enforcement (see confam/payout_accounts.py — get_active_payout_account()
-- already uses ORDER BY active_from DESC LIMIT 1 to find the right row,
-- and the change endpoint rejects a second pending change explicitly).
--
-- WHY NOT KEEP THE INDEX:
--   Postgres IMMUTABILITY rules prevent using now() in an index predicate,
--   so there is no clean way to express "at most one row where active_from
--   has passed" as a partial unique index. Application-layer enforcement
--   is the right home for this constraint.
--
-- RULE 5 NOTE:
--   superseded_by on the old row is set lazily — only when the new row's
--   active_from passes and the change becomes effective. During the cooling-off
--   window, both rows have superseded_by = NULL. This is intentional:
--   setting superseded_by at request time would immediately deactivate the old
--   account, defeating the whole purpose of the cooling-off period.
-- =============================================================================

DROP INDEX IF EXISTS payout_accounts_one_active_per_merchant;

-- Add a column to track whether a row is a pending change request.
-- NULL  = normal row (either active or superseded history)
-- future TIMESTAMPTZ = pending change requested at this time, not yet active
-- This is informational/audit only — the live/pending distinction is determined
-- purely by active_from <= now() at query time.
ALTER TABLE payout_accounts
    ADD COLUMN IF NOT EXISTS change_requested_at TIMESTAMPTZ;

COMMENT ON COLUMN payout_accounts.change_requested_at IS
    'Set when this row was inserted as a pending payout-account change request. '
    'NULL for onboarding rows and for rows that were activated without a cooling-off period. '
    'Informational only — whether the row is currently active is determined by '
    'active_from <= now() AND superseded_by IS NULL at query time.';
