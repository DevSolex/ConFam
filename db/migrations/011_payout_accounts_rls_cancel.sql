-- =============================================================================
-- Migration: 011_payout_accounts_rls_cancel
--
-- Enables RLS on payout_accounts. DELETE policy restricts confam_app to
-- pending rows only (active_from > now()).
--
-- Conditional: role-specific policies only applied when confam_app exists.
-- On Render (single-role), migration 012 creates PUBLIC policies instead.
-- FORCE ROW LEVEL SECURITY applies to the table owner regardless of role.
-- =============================================================================

-- Enable RLS on payout_accounts (applies to all users including owner via FORCE).
ALTER TABLE payout_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE payout_accounts FORCE ROW LEVEL SECURITY;

-- Apply role-specific policies only when confam_app exists (standard deployment).
-- On Render, migration 012 creates equivalent PUBLIC policies.
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'confam_app') THEN

    CREATE POLICY payout_accounts_select_policy
        ON payout_accounts FOR SELECT TO confam_app USING (true);

    CREATE POLICY payout_accounts_insert_policy
        ON payout_accounts FOR INSERT TO confam_app WITH CHECK (true);

    CREATE POLICY payout_accounts_update_policy
        ON payout_accounts FOR UPDATE TO confam_app USING (true);

    -- THE KEY POLICY: DELETE restricted to pending rows (active_from > now())
    CREATE POLICY payout_accounts_delete_pending_only
        ON payout_accounts FOR DELETE TO confam_app
        USING (active_from > now());

    GRANT DELETE ON payout_accounts TO confam_app;

    RAISE NOTICE 'RLS policies and GRANT DELETE applied for confam_app.';

  ELSE
    RAISE NOTICE 'confam_app not found — skipping role-specific RLS policies (Render single-role). Migration 012 will apply PUBLIC policies.';
  END IF;
END
$$;

COMMENT ON TABLE payout_accounts IS
    'Merchant payout accounts. Append-only for active/historical rows (Rule 2). '
    'RLS enabled with FORCE. DELETE restricted to active_from > now() rows only. '
    'On standard deployments: confam_app-specific policies. '
    'On Render: PUBLIC policies via migration 012.';
