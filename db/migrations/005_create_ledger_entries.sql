-- =============================================================================
-- Migration: 005_create_ledger_entries
-- Creates the LedgerEntry entity — the core product of ConFam.
-- See docs/DATA_MODEL.md §1 (LedgerEntry), §3, and §4.
--
-- RULE ENCODING:
--   Engineering Rule 2 (append-only): UPDATE and DELETE on this table are
--   structurally blocked via Postgres RULEs below. These are not application
--   conventions — they are enforced at the Postgres level so no code path,
--   ORM, admin script, or direct psql session can mutate a ledger row.
--
--   Corrections are made by inserting a new row with entry_type = 'correction'
--   that references the original row via corrects_entry_id. The original row
--   is NEVER touched.
--
--   Engineering Rule 6 (exact arithmetic): amount_minor_units is BIGINT (kobo).
--   conversion_rate is NUMERIC(20,10) — exact decimal, not FLOAT.
-- =============================================================================

CREATE TABLE ledger_entries (
    ledger_entry_id     UUID            PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Full traceability chain (Engineering Rule 4):
    --   ledger_entry_id → rail_event_id → link_id → merchant_id → payout_account_id
    link_id             UUID            NOT NULL REFERENCES payment_links(link_id),
    merchant_id         UUID            NOT NULL REFERENCES merchants(merchant_id),
    -- Snapshot of which whitelisted account received this payout.
    -- Even if the merchant later changes their payout account, this row always
    -- records which account was paid for THIS transaction.
    payout_account_id   UUID            NOT NULL REFERENCES payout_accounts(payout_account_id),
    -- The exact rail event that triggered this ledger entry.
    rail_event_id       UUID            NOT NULL REFERENCES rail_events(rail_event_id),

    -- Integer, smallest currency unit (kobo). NEVER a float (Engineering Rule 6).
    amount_minor_units  BIGINT          NOT NULL CHECK (amount_minor_units > 0),
    currency            TEXT            NOT NULL DEFAULT 'NGN',

    rail                TEXT            NOT NULL,
    CONSTRAINT ledger_entries_rail_check CHECK (
        rail IN ('bank', 'stellar')
    ),

    -- Populated only when the buyer paid in stablecoin (Stellar rail).
    -- Stored as exact NUMERIC, not FLOAT (Engineering Rule 6).
    -- Recorded once at settlement time; never recomputed retroactively.
    conversion_rate     NUMERIC(20, 10),
    conversion_rate_source TEXT,  -- e.g. 'stellar_anchor_rate_2026-09-17T09:19:46Z'

    confirmed_at        TIMESTAMPTZ     NOT NULL,

    -- 'sale' for normal transactions; 'correction' for reconciliation corrections.
    -- Corrections reference the original row via corrects_entry_id — the original
    -- row is NEVER modified (Engineering Rule 2).
    entry_type          TEXT            NOT NULL DEFAULT 'sale',
    CONSTRAINT ledger_entries_entry_type_check CHECK (
        entry_type IN ('sale', 'correction')
    ),

    -- Only set when entry_type = 'correction'. Points to the row being corrected.
    corrects_entry_id   UUID            REFERENCES ledger_entries(ledger_entry_id),

    -- created_at intentionally named differently from confirmed_at:
    -- confirmed_at = when the rail confirmed payment; created_at = when this row was written.
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now()
);

-- Each rail_event_id should produce at most one 'sale' ledger entry.
-- This prevents the application layer from accidentally creating duplicate entries
-- even if the RailEvent idempotency constraint somehow let a duplicate through.
CREATE UNIQUE INDEX ledger_entries_one_sale_per_rail_event
    ON ledger_entries (rail_event_id)
    WHERE entry_type = 'sale';

-- =============================================================================
-- APPEND-ONLY ENFORCEMENT (Engineering Rule 2)
--
-- These Postgres RULEs structurally block UPDATE and DELETE on ledger_entries.
-- They fire before any write, so no client, ORM, admin script, or direct psql
-- session can mutate a ledger row — including database administrators running
-- ad-hoc queries.
--
-- To correct a ledger entry: INSERT a new row with entry_type = 'correction'
-- and corrects_entry_id pointing to the original. Never UPDATE or DELETE.
-- =============================================================================

CREATE RULE ledger_entries_no_update AS
    ON UPDATE TO ledger_entries
    DO INSTEAD NOTHING;

CREATE RULE ledger_entries_no_delete AS
    ON DELETE TO ledger_entries
    DO INSTEAD NOTHING;

COMMENT ON TABLE ledger_entries IS
    'THE PRODUCT. Append-only naira-denominated record of every confirmed sale. '
    'UPDATE and DELETE are blocked by Postgres RULEs — this is structural, not conventional. '
    'Corrections are new rows (entry_type=correction) referencing the original. '
    'See docs/DATA_MODEL.md and ENGINEERING_RULES.md Rules 2, 3, 4, 6.';

COMMENT ON COLUMN ledger_entries.amount_minor_units IS
    'Amount in kobo (smallest NGN unit). BIGINT — never FLOAT (Engineering Rule 6).';

COMMENT ON COLUMN ledger_entries.conversion_rate IS
    'NUMERIC(20,10) — exact decimal, not FLOAT. Populated once at settlement time '
    'for Stellar/stablecoin transactions. Never recomputed retroactively (Rule 6).';

COMMENT ON COLUMN ledger_entries.payout_account_id IS
    'Snapshot FK: records which whitelisted account was paid for THIS transaction. '
    'Immutable even if the merchant later changes their payout account.';
