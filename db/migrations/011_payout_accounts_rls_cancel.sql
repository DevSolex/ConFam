-- =============================================================================
-- Migration: 011_payout_accounts_rls_cancel
--
-- Replaces the db_migrator.py elevated-credential pattern for
-- cancel_pending_change() with Row-Level Security.
--
-- WHAT THIS DOES:
--   Enables RLS on payout_accounts and adds a DELETE policy that allows
--   confam_app to delete ONLY rows where active_from > now() — i.e., rows
--   that have never gone live. Any row that has ever been the active payout
--   account (active_from <= now()) is structurally undeletable by confam_app,
--   regardless of application code.
--
-- WHY RLS INSTEAD OF A SECOND CREDENTIAL:
--   db_migrator.py gave the running settlement-engine service a second,
--   migrator-level connection — reintroducing exactly what OQ-018 deliberately
--   prevented. Even in a separate module, the migrator credential must exist
--   in the service's runtime environment, making it reachable if the service
--   is compromised. RLS enforces the constraint structurally at the database
--   layer with no elevated credential required at runtime.
--
-- POLICY SCOPE:
--   The DELETE policy uses USING (active_from > now()) — applies only to DELETE.
--   Existing SELECT, INSERT, and column-level UPDATE grants are unaffected.
--   confam_app cannot delete any row where active_from IS NULL or <= now().
--
-- NOTE: RLS DOES NOT APPLY TO SUPERUSERS OR TABLE OWNERS BY DEFAULT.
--   confam_migrator (which owns or has BYPASSRLS) is still able to delete any
--   row for maintenance purposes. The trigger on ledger_entries still blocks
--   UPDATE/DELETE there regardless of role. This policy only governs confam_app.
-- =============================================================================

-- Enable Row-Level Security on payout_accounts.
-- FORCE ensures the policy applies even to the table owner.
ALTER TABLE payout_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE payout_accounts FORCE ROW LEVEL SECURITY;

-- Allow confam_app to SELECT all rows (restores visibility after RLS enabled).
CREATE POLICY payout_accounts_select_policy
    ON payout_accounts
    FOR SELECT
    TO confam_app
    USING (true);

-- Allow confam_app to INSERT new rows (no restriction on inserts).
CREATE POLICY payout_accounts_insert_policy
    ON payout_accounts
    FOR INSERT
    TO confam_app
    WITH CHECK (true);

-- Allow confam_app to UPDATE only the columns it already has grants for.
-- The USING clause here is permissive (any row) — column-level grants
-- from migration 007 are the actual restriction on what gets updated.
CREATE POLICY payout_accounts_update_policy
    ON payout_accounts
    FOR UPDATE
    TO confam_app
    USING (true);

-- =============================================================================
-- THE KEY POLICY: DELETE restricted to pending (future active_from) rows only.
--
-- confam_app can delete a row ONLY if active_from > now().
-- This means:
--   - Pending change rows (active_from in the future): deletable — correct,
--     this is cancel_pending_change()'s job.
--   - Active rows (active_from <= now()): NOT deletable — the policy USING
--     clause evaluates false and Postgres rejects the DELETE.
--   - Rows with active_from IS NULL: NOT deletable (NULL > now() is NULL,
--     which is falsy in USING) — these are partially-verified rows that
--     should never be deleted either.
--
-- This is enforced at the database level, not in application code.
-- confam_app can now be granted DELETE on this table without risk of
-- deleting historical or currently-active payout accounts.
-- =============================================================================
CREATE POLICY payout_accounts_delete_pending_only
    ON payout_accounts
    FOR DELETE
    TO confam_app
    USING (active_from > now());

-- Grant DELETE to confam_app. The RLS policy above is the structural guard
-- that ensures this only applies to pending (never-active) rows.
GRANT DELETE ON payout_accounts TO confam_app;

-- NOTE: confam_migrator needs BYPASSRLS to apply schema changes without
-- being blocked by the policies above. This is granted by the bootstrap
-- script (db/bootstrap_roles.sh) using the postgres superuser — it cannot
-- be done here because ALTER ROLE ... BYPASSRLS requires superuser privilege
-- and confam_migrator is not a superuser.

COMMENT ON TABLE payout_accounts IS
    'Merchant payout accounts. Append-only for active/historical rows (Rule 2). '
    'RLS enabled: confam_app can only DELETE rows where active_from > now() '
    '(pending change rows that have never gone live). '
    'Active or historical rows (active_from <= now()) are structurally undeletable '
    'by confam_app — the RLS policy rejects the DELETE before it reaches the trigger. '
    'confam_migrator bypasses RLS for maintenance.';
