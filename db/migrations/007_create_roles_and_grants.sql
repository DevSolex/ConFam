-- =============================================================================
-- Migration: 007_create_roles_and_grants
--
-- Assigns grants to the confam_app role.
-- Conditional: skipped on Render (single-role deployment, no confam_app).
-- On Render, the single DB owner is used for everything.
-- Migration 012 handles the Render-specific RLS adaptation.
--
-- This file does NOT create roles. Role creation lives in db/bootstrap_roles.sh
-- (Docker/EC2) and in the CI workflow step, both of which run as a superuser.
-- It must not happen here: a schema migration that can CREATE ROLE will create
-- confam_app on any deployment whose user happens to hold CREATEROLE — including
-- Render — which silently flips migrations 011/012 into their "standard
-- deployment" branch. Those create RLS policies scoped TO confam_app, and
-- payout_accounts is FORCE ROW LEVEL SECURITY, so the Render app (which connects
-- as the single DB user, not confam_app) would see zero rows and be denied
-- inserts. Roles are therefore created outside the migration chain, and this
-- file only grants — gated on confam_app existing, the same convention used by
-- migrations 006, 011 and 012.
--
-- Grants additionally require the current user to own the objects, which holds
-- on both paths: confam_migrator owns the tables it created, and on Render the
-- single DB user owns them (but has no confam_app to grant to, so it skips).
--
-- This file is IDEMPOTENT and may be re-applied to an existing database
-- (Alembic revision 0007 does exactly that to converge databases that were
-- stamped before the column-level guards below existed).
-- =============================================================================

DO $$
BEGIN

  -- Grants below are skipped on Render, where confam_app does not exist.
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'confam_app') THEN
    RAISE NOTICE 'confam_app role not found — skipping grants (Render single-role deployment). Trigger and FORCE RLS are the active defences.';
    RETURN;
  END IF;

  -- merchants
  GRANT SELECT, INSERT ON merchants TO confam_app;
  GRANT UPDATE (status) ON merchants TO confam_app;

  -- payout_accounts
  GRANT SELECT, INSERT ON payout_accounts TO confam_app;
  -- superseded_by exists from migration 002; change_requested_at is added by
  -- migration 010, which runs in a LATER Alembic revision. A GRANT naming a
  -- missing column raises "column does not exist" and aborts the whole
  -- transaction, so grant only the columns present at this point. Revision
  -- 0007 re-runs this file once 010 has been applied.
  IF EXISTS (
    SELECT FROM pg_attribute
    WHERE attrelid = 'payout_accounts'::regclass
      AND attname = 'change_requested_at'
      AND NOT attisdropped
  ) THEN
    GRANT UPDATE (superseded_by, change_requested_at) ON payout_accounts TO confam_app;
  ELSE
    GRANT UPDATE (superseded_by) ON payout_accounts TO confam_app;
    RAISE NOTICE 'payout_accounts.change_requested_at not present yet — granted superseded_by only.';
  END IF;

  -- payment_links
  GRANT SELECT, INSERT ON payment_links TO confam_app;
  GRANT UPDATE (status) ON payment_links TO confam_app;

  -- rail_events
  GRANT SELECT, INSERT ON rail_events TO confam_app;
  GRANT UPDATE (processed) ON rail_events TO confam_app;

  -- ledger_entries
  GRANT SELECT, INSERT ON ledger_entries TO confam_app;
  REVOKE UPDATE, DELETE ON ledger_entries FROM confam_app;

  -- sequences
  GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO confam_app;

  -- confam_migrator: full DDL/DML (bootstrap_roles.sh normally grants this
  -- as superuser before alembic runs; repeated here for idempotence)
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'confam_migrator') THEN
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO confam_migrator;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO confam_migrator;
    GRANT CREATE ON SCHEMA public TO confam_migrator;
  END IF;

  RAISE NOTICE 'Roles and grants applied (standard deployment).';

END
$$;
