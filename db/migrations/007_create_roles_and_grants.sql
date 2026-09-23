-- =============================================================================
-- Migration: 007_create_roles_and_grants
--
-- Creates the two ConFam database roles and assigns the minimum necessary
-- privileges to each. Resolves OPEN_QUESTIONS.md OQ-018.
--
-- ROLES:
--   confam_app      — the role the running application connects as.
--                     Minimum privileges only; no UPDATE/DELETE on ledger.
--   confam_migrator — used only by Alembic and DBAs for schema changes.
--                     Full DDL/DML, but still blocked on ledger_entries
--                     by the trigger from migration 006 (second line of
--                     defence for this role specifically).
--
-- PRINCIPLE OF LEAST PRIVILEGE (Engineering Rule 8):
--   confam_app holds no DELETE anywhere and no DDL at all.
--   UPDATE is granted only at the column level, only where the state machine
--   genuinely requires it — not as full-table UPDATE.
--   Stellar signing keys and Paystack secret keys never appear in the DB;
--   they live in AWS Secrets Manager (OQ-007, Rule 8).
--
-- confam_migrator MUST NEVER be used as the application runtime connection.
-- The DATABASE_URL in .env / AWS Secrets Manager must connect as confam_app.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Create roles (idempotent: DO NOTHING if already exists)
-- -----------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'confam_app') THEN
        CREATE ROLE confam_app WITH LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'confam_migrator') THEN
        CREATE ROLE confam_migrator WITH LOGIN;
    END IF;
END
$$;


-- =============================================================================
-- confam_app grants
-- =============================================================================

-- merchants
-- confam_app may read all columns and insert new merchants.
-- UPDATE is column-scoped to status only (onboarding state transitions).
-- No DELETE.
GRANT SELECT, INSERT ON merchants TO confam_app;
GRANT UPDATE (status) ON merchants TO confam_app;

-- payout_accounts
-- confam_app may read and insert (new rows supersede old ones — Rule 2/5).
-- No UPDATE at all: superseded_by is set only via insert-a-new-row workflow.
GRANT SELECT, INSERT ON payout_accounts TO confam_app;

-- payment_links
-- confam_app may read, insert, and update status column only (state machine).
-- No UPDATE on other columns (amount, expiry, etc. are immutable after creation).
GRANT SELECT, INSERT ON payment_links TO confam_app;
GRANT UPDATE (status) ON payment_links TO confam_app;

-- rail_events
-- confam_app may read and insert. UPDATE is column-scoped to processed only
-- (flipped to TRUE after a LedgerEntry is written — Rule 3 idempotency flag).
GRANT SELECT, INSERT ON rail_events TO confam_app;
GRANT UPDATE (processed) ON rail_events TO confam_app;

-- ledger_entries
-- confam_app may SELECT and INSERT only.
-- No UPDATE, no DELETE — these are revoked (primary defence).
-- The trigger in migration 006 is the second line of defence for other roles.
-- This REVOKE is the primary defence for confam_app specifically.
GRANT SELECT, INSERT ON ledger_entries TO confam_app;
REVOKE UPDATE, DELETE ON ledger_entries FROM confam_app;

-- Sequences (needed for INSERT on UUID-defaulted tables if using serial fallback)
-- If all PKs are gen_random_uuid() this is a no-op, but it's explicit for safety.
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO confam_app;


-- =============================================================================
-- confam_migrator grants
-- =============================================================================

-- confam_migrator needs full DDL and DML to apply Alembic migrations and to
-- write correction rows to the ledger (Rule 2: corrections are new INSERT rows).
-- The trigger on ledger_entries (migration 006) still blocks UPDATE/DELETE
-- even for this role — that trigger is the backstop for confam_migrator.
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO confam_migrator;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO confam_migrator;

-- Grant DDL privileges (CREATE, DROP, ALTER) on the schema itself.
GRANT CREATE ON SCHEMA public TO confam_migrator;

-- Ensure future tables created by migrations are also accessible.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON TABLES TO confam_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON SEQUENCES TO confam_migrator;

-- confam_app should inherit SELECT/INSERT on tables created by future migrations.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT ON TABLES TO confam_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO confam_app;


-- =============================================================================
-- Reminder comment — do not remove
-- =============================================================================
-- The DATABASE_URL used by the running application (settlement engine, checkout,
-- messaging services) MUST connect as confam_app, not confam_migrator.
-- Alembic's sqlalchemy.url in alembic.ini should use confam_migrator.
-- These are two separate connection strings; they must never be swapped.
-- See .env.example for the correct variable names.
-- =============================================================================
