-- =============================================================================
-- Migration: 002_create_payout_accounts
-- Creates the PayoutAccount entity.
-- See docs/DATA_MODEL.md §1 (PayoutAccount) for field-level notes.
--
-- RULE ENCODING:
--   Engineering Rule 2 (append-only): rows in this table are NEVER updated or
--   deleted. A new payout account supersedes an old one by setting superseded_by
--   on the old row — but even that "update" must go through the audit-safe path
--   defined in the application layer.
--
--   Engineering Rule 5 (payout address changes require re-verification and a
--   cooling-off period): active_from is only set after the cooling-off window
--   elapses. The settlement engine MUST NOT read a PayoutAccount as active until
--   active_from <= now() AND superseded_by IS NULL.
--
--   Engineering Rule 9 (personal/financial data encrypted at rest):
--   bank_account_number and bank_code are encrypted by the application layer
--   before write. The schema stores ciphertext; decryption happens only in the
--   service, never in SQL. Access to raw values must be logged (Rule 9).
-- =============================================================================

CREATE TABLE payout_accounts (
    payout_account_id   UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         UUID            NOT NULL REFERENCES merchants(merchant_id),

    -- Encrypted at rest by the application layer (Engineering Rule 9).
    -- Schema stores ciphertext. Never store plaintext bank details here.
    bank_account_number TEXT            NOT NULL,  -- ciphertext
    bank_code           TEXT            NOT NULL,  -- ciphertext

    -- How ownership was verified. See ENGINEERING_RULES.md Rule 5.
    -- e.g. 'micro_deposit', 'bank_api_resolve'
    verification_method TEXT            NOT NULL,
    verified_at         TIMESTAMPTZ,

    -- Set only after the cooling-off window elapses (Rule 5).
    -- NULL means the account is pending activation.
    active_from         TIMESTAMPTZ,

    -- Points to the row that superseded this one, if any.
    -- The settlement engine treats a row as the CURRENT active account only when:
    --   active_from <= now() AND superseded_by IS NULL
    -- This is never set directly by application writes to this table;
    -- the supersession workflow in the settlement engine manages it.
    superseded_by       UUID            REFERENCES payout_accounts(payout_account_id),

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now()

    -- NOTE: No updated_at column intentionally. This table is effectively
    -- append-only (Rule 2). The only mutable column is superseded_by, and that
    -- mutation is a constrained, audited operation, not a general update.
);

-- A merchant can have only one unsuperseded, active payout account at a time.
-- This partial unique index enforces that at the database level.
-- The predicate checks that the row has not been superseded AND has been activated
-- (active_from IS NOT NULL). The timing check (active_from <= now()) is enforced
-- by the application layer, not here, because now() is not IMMUTABLE and cannot
-- be used in an index predicate.
CREATE UNIQUE INDEX payout_accounts_one_active_per_merchant
    ON payout_accounts (merchant_id)
    WHERE superseded_by IS NULL AND active_from IS NOT NULL;

COMMENT ON TABLE payout_accounts IS
    'Merchant payout accounts. Append-only (Engineering Rule 2). '
    'bank_account_number and bank_code are ciphertext — application decrypts, never SQL. '
    'The settlement engine may only pay to a row where active_from <= now() AND superseded_by IS NULL.';

COMMENT ON COLUMN payout_accounts.superseded_by IS
    'FK to the row that replaced this account. When NULL and active_from is set, '
    'this is the current active payout account for the merchant.';
