-- =============================================================================
-- Migration: 001_create_merchants
-- Creates the Merchant entity.
-- See docs/DATA_MODEL.md §1 (Merchant) for field-level notes.
-- =============================================================================

CREATE TABLE merchants (
    merchant_id         UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The merchant's own buyer-facing WhatsApp number (thread 1).
    -- Informational only — ConFam NEVER messages through this number (ARCHITECTURE.md §1).
    whatsapp_number     TEXT            NOT NULL,
    -- The merchant's ConFam thread identity (thread 2 — the only thread ConFam participates in).
    confam_thread_id    TEXT            NOT NULL UNIQUE,
    -- Tracks how deeply the merchant has been verified; allows strengthening over time
    -- without a schema rewrite (COMPLIANCE_SECURITY.md §4).
    -- e.g. 'unverified', 'basic_kyb', 'enhanced_kyb'
    verification_tier   TEXT            NOT NULL DEFAULT 'unverified',
    status              TEXT            NOT NULL DEFAULT 'pending_verification',
    -- CHECK constraint keeps the status column to the defined state machine values.
    CONSTRAINT merchants_status_check CHECK (
        status IN ('pending_verification', 'active', 'suspended')
    ),
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT now()
);

COMMENT ON TABLE merchants IS
    'Merchants using ConFam. One merchant maps to one ConFam thread (thread 2). '
    'ConFam never writes to or reads the merchant''s buyer-facing thread (thread 1).';

COMMENT ON COLUMN merchants.whatsapp_number IS
    'The merchant''s own number for buyer conversations. '
    'ConFam does not message this number — stored for reference/support only.';
