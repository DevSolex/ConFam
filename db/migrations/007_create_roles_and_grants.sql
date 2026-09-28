-- =============================================================================
-- Migration: 007_create_roles_and_grants
--
-- Creates confam_app and confam_migrator roles and assigns grants.
-- Conditional: skipped entirely on Render (no superuser/createrole privilege).
-- On Render, the single DB owner is used for everything.
-- Migration 012 handles the Render-specific RLS adaptation.
-- =============================================================================

DO $$
DECLARE
  has_privilege BOOLEAN;
BEGIN
  -- Check if current user can create roles (requires superuser or createrole)
  SELECT (rolsuper OR rolcreaterole) INTO has_privilege
  FROM pg_roles WHERE rolname = current_user;

  IF has_privilege THEN

    -- Create roles if they don't exist
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'confam_app') THEN
      CREATE ROLE confam_app WITH LOGIN;
      RAISE NOTICE 'Created role confam_app';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'confam_migrator') THEN
      CREATE ROLE confam_migrator WITH LOGIN;
      RAISE NOTICE 'Created role confam_migrator';
    END IF;

    -- merchants
    GRANT SELECT, INSERT ON merchants TO confam_app;
    GRANT UPDATE (status) ON merchants TO confam_app;

    -- payout_accounts
    GRANT SELECT, INSERT ON payout_accounts TO confam_app;
    GRANT UPDATE (superseded_by, change_requested_at) ON payout_accounts TO confam_app;

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

    -- confam_migrator: full DDL/DML
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO confam_migrator;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO confam_migrator;
    GRANT CREATE ON SCHEMA public TO confam_migrator;

    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT ALL ON TABLES TO confam_migrator;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT ALL ON SEQUENCES TO confam_migrator;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT ON TABLES TO confam_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE ON SEQUENCES TO confam_app;

    RAISE NOTICE 'Roles and grants applied (standard deployment).';

  ELSE
    RAISE NOTICE 'No superuser/createrole privilege — skipping role creation and grants (Render single-role deployment). Trigger and FORCE RLS are the active defences.';
  END IF;

END
$$;
