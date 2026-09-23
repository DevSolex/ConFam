-- =============================================================================
-- Migration: 004_create_rail_events
-- Creates the RailEvent entity.
-- See docs/DATA_MODEL.md §1 (RailEvent) and §3 (why RailEvent and LedgerEntry
-- are separate tables).
--
-- RULE ENCODING:
--   Engineering Rule 3 (idempotency): the UNIQUE constraint on (rail, rail_reference)
--   is the mechanical enforcement of the no-double-count guarantee. A duplicate
--   webhook from either rail will be rejected by the database itself before the
--   application layer can double-process it.
--
-- This table holds raw, possibly-duplicated rail confirmations. Only after
-- successful idempotency check and LedgerEntry write does `processed` flip true.
-- =============================================================================

CREATE TABLE rail_events (
    rail_event_id       UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    link_id             UUID            NOT NULL REFERENCES payment_links(link_id),

    -- Which rail sent this event.
    rail                TEXT            NOT NULL,
    CONSTRAINT rail_events_rail_check CHECK (
        rail IN ('bank', 'stellar')
    ),

    -- The unique reference from the rail itself — this is the idempotency key.
    -- For bank rail: the aggregator's transaction reference.
    -- For Stellar rail: the Stellar transaction hash.
    rail_reference      TEXT            NOT NULL,

    -- Raw payload as received, kept for reconciliation (Engineering Rule 7).
    -- Stored as JSONB so structured queries are possible during reconciliation.
    raw_payload         JSONB           NOT NULL,

    received_at         TIMESTAMPTZ     NOT NULL DEFAULT now(),

    -- Flips to TRUE only after the corresponding LedgerEntry has been
    -- successfully written. The settlement engine checks this flag as part of
    -- idempotency handling — do not process a rail_event_id whose processed = TRUE.
    processed           BOOLEAN         NOT NULL DEFAULT FALSE
);

-- =============================================================================
-- THE MOST IMPORTANT CONSTRAINT IN THE ENTIRE SCHEMA.
--
-- This UNIQUE constraint is the database-level enforcement of Engineering Rule 3
-- (idempotency) and the foundation of the "50+ transaction no-double-count" test.
--
-- A rail WILL retry webhook delivery. When it does, the INSERT of the duplicate
-- (rail, rail_reference) pair will fail here with a unique violation — before
-- any application-layer settlement logic runs.
--
-- DO NOT DROP OR DISABLE THIS CONSTRAINT. If a migration ever proposes removing
-- it, treat that as a Rule 3 violation and reject the PR.
-- =============================================================================
CREATE UNIQUE INDEX rail_events_idempotency_key
    ON rail_events (rail, rail_reference);

COMMENT ON TABLE rail_events IS
    'Raw confirmation events from payment rails. '
    'The UNIQUE constraint on (rail, rail_reference) is the mechanical enforcement '
    'of Engineering Rule 3 (idempotency / no-double-count). DO NOT REMOVE IT.';

COMMENT ON COLUMN rail_events.rail_reference IS
    'Unique reference from the rail: aggregator tx ref (bank) or Stellar tx hash (stellar). '
    'This is the idempotency key. The UNIQUE index on (rail, rail_reference) enforces Rule 3.';

COMMENT ON COLUMN rail_events.processed IS
    'TRUE only after a LedgerEntry has been successfully written for this event. '
    'Settlement engine must not re-process a rail_event where processed = TRUE.';
