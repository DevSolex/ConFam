-- =============================================================================
-- Migration: 012_render_single_role_adaptation
--
-- Applied on Render where only one DB user exists (the DB owner).
-- Rewrites the role-specific RLS policies from migration 011 to work
-- without the confam_app / confam_migrator role split.
--
-- On Render:
--   - No CREATE ROLE (requires superuser)
--   - No ALTER ROLE BYPASSRLS (requires superuser)
--   - FORCE ROW LEVEL SECURITY still applies to the table owner, so RLS
--     is still enforced structurally — the policy logic is preserved,
--     just scoped to PUBLIC rather than a specific role.
--
-- What is preserved:
--   1. Append-only trigger on ledger_entries (fires for ALL users).
--   2. FORCE ROW LEVEL SECURITY on payout_accounts (owner is subject to policy).
--   3. DELETE restricted to active_from > now() — the actual security guarantee.
--
-- What differs from local Docker setup:
--   - Policies are for PUBLIC (all users) instead of confam_app specifically.
--   - The single Render DB user can technically do more than confam_app could.
--   - This is documented as a known gap for the pilot deployment.
--   - When migrating to a dedicated Postgres instance (OQ-007 / paid tier),
--     the confam_app/confam_migrator split should be restored.
--
-- This migration is conditional — it only applies meaningful changes on
-- Render where confam_app role does not exist.
-- =============================================================================

DO $$
BEGIN
    -- Only run this adaptation if confam_app role does NOT exist
    -- (i.e., this is a Render deployment without the role split)
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'confam_app') THEN

        -- Drop the role-specific RLS policies from migration 011
        DROP POLICY IF EXISTS payout_accounts_select_policy ON payout_accounts;
        DROP POLICY IF EXISTS payout_accounts_insert_policy ON payout_accounts;
        DROP POLICY IF EXISTS payout_accounts_update_policy ON payout_accounts;
        DROP POLICY IF EXISTS payout_accounts_delete_pending_only ON payout_accounts;

        -- Recreate policies for PUBLIC (applies to all users including DB owner)
        -- RLS is already FORCE, so even the table owner is subject to these.

        CREATE POLICY payout_accounts_select_policy
            ON payout_accounts FOR SELECT USING (true);

        CREATE POLICY payout_accounts_insert_policy
            ON payout_accounts FOR INSERT WITH CHECK (true);

        CREATE POLICY payout_accounts_update_policy
            ON payout_accounts FOR UPDATE USING (true);

        -- THE KEY POLICY: DELETE only allowed for pending rows (active_from > now())
        -- This is the structural guarantee that survives the single-role model.
        CREATE POLICY payout_accounts_delete_pending_only
            ON payout_accounts FOR DELETE USING (active_from > now());

        RAISE NOTICE 'Render adaptation: RLS policies rewritten for PUBLIC (single-role deployment)';

    ELSE
        RAISE NOTICE 'confam_app role exists — Render adaptation not needed (standard deployment)';
    END IF;
END
$$;
