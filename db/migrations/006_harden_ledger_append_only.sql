-- =============================================================================
-- Migration: 006_harden_ledger_append_only
--
-- Replaces the Postgres RULE-based append-only enforcement on ledger_entries
-- with two stronger, complementary mechanisms:
--
--   1. A BEFORE UPDATE OR DELETE trigger that raises an exception unconditionally.
--   2. A REVOKE of UPDATE and DELETE privileges from the application role.
--
-- WHY THIS REPLACES THE RULE BLOCKS (Engineering Rule 2 — hardening pass):
--
--   Postgres RULEs with DO INSTEAD NOTHING silently swallow UPDATE/DELETE:
--   the operation appears to succeed (no error), rows are not mutated, but
--   any RETURNING clause comes back empty and the caller has no way to
--   distinguish "row was updated" from "row was silently discarded." This
--   means ORMs, application code, and reconciliation scripts can issue a
--   mutating statement and receive a misleading success signal.
--
--   A BEFORE trigger that raises an exception is the idiomatic Postgres
--   pattern for immutable tables: it produces a hard error that cannot be
--   mistaken for success, propagates through every client (ORMs, psql,
--   application drivers), and aborts the enclosing transaction.
--
--   Revoking the privilege from the application role is the primary defence:
--   the app's connection cannot even attempt the operation. The trigger is
--   the second line of defence for privileged roles (e.g. a migration runner
--   or DBA session) that legitimately hold broader grants.
--
-- Together they enforce Rule 2 at two independent layers, so Rule 2 holds
-- even if one layer is misconfigured.
--
-- NOTE ON ROLE NAME:
--   The application role is `confam_app`. It is created with full grants in
--   migration 007_create_roles_and_grants.sql (OQ-018 resolved in DECISIONS.md).
--   Migration 007 must run before or together with this migration in any
--   environment where confam_app does not yet exist.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Step 1: Drop the RULE blocks introduced in migration 005.
-- -----------------------------------------------------------------------------

DROP RULE IF EXISTS ledger_entries_no_update ON ledger_entries;
DROP RULE IF EXISTS ledger_entries_no_delete ON ledger_entries;


-- -----------------------------------------------------------------------------
-- Step 2: Trigger function — raises a hard exception on any UPDATE or DELETE.
--
-- WHY A TRIGGER INSTEAD OF RULES (Engineering Rule 2, hardening pass):
--   Rules with DO INSTEAD NOTHING produce a silent no-op that looks like
--   success to the caller. A trigger that raises an exception produces a
--   hard, transaction-aborting error visible to every client and driver.
--   This is the standard Postgres pattern for immutable tables and avoids
--   the RETURNING/ON CONFLICT/multi-row edge cases that plague RULE-based
--   approaches.
--
-- This function applies to ALL roles, including migration runners and DBAs.
-- The application role's privilege revocation (Step 3) is the primary guard;
-- this trigger is the second line of defence for any role that retains
-- broader grants on this table.
--
-- To correct a ledger entry: INSERT a new row with entry_type = 'correction'
-- and corrects_entry_id pointing to the original. Never UPDATE or DELETE.
-- See docs/DATA_MODEL.md §1 (LedgerEntry) and ENGINEERING_RULES.md Rule 2.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION ledger_entries_enforce_append_only()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'ledger_entries is append-only (Engineering Rule 2): % is not permitted. '
        'To correct a ledger entry, INSERT a new row with entry_type = ''correction'' '
        'and corrects_entry_id pointing to the original row. '
        'See docs/DATA_MODEL.md §1 and ENGINEERING_RULES.md Rule 2.',
        TG_OP;
END;
$$;

COMMENT ON FUNCTION ledger_entries_enforce_append_only() IS
    'Raises an exception on any UPDATE or DELETE against ledger_entries. '
    'Enforces Engineering Rule 2 (append-only ledger) at the database level '
    'for all roles. Corrections must be new rows (entry_type=correction). '
    'Added by migration 006 to replace the RULE-based enforcement in migration 005.';


-- Attach the trigger to the table.
-- BEFORE ensures the exception fires before any row is touched,
-- so the statement is always rolled back cleanly.
CREATE TRIGGER ledger_entries_append_only_trigger
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_enforce_append_only();

COMMENT ON TRIGGER ledger_entries_append_only_trigger ON ledger_entries IS
    'Fires BEFORE UPDATE OR DELETE on every row. Always raises an exception. '
    'Second line of defence after privilege revocation (Step 3 of migration 006). '
    'Covers any role that retains broader grants, e.g. migration runner, DBA. '
    'Engineering Rule 2.';


-- -----------------------------------------------------------------------------
-- Step 3: Revoke UPDATE and DELETE privileges from the application role.
--
-- WHY REVOKE IN ADDITION TO THE TRIGGER (Engineering Rule 2, primary defence):
--   The trigger fires for any role, but privilege revocation is the stronger
--   guarantee: the application's database connection cannot even parse a
--   mutating statement against this table into the planner — the rejection
--   happens before query execution, before the trigger, and before any
--   application-layer logic. This means a future ORM version, a dependency
--   bug, or a raw psql session under the application's own credentials cannot
--   accidentally mutate a ledger row even if the trigger were somehow removed.
--
-- The trigger (Step 2) remains as the second line of defence for privileged
-- roles (migration runner, DBA) that legitimately hold broader grants.
--
-- TODO (OPEN_QUESTIONS.md OQ-018 — RESOLVED): `confam_app` role is created in
--   migration 007. Migration 007 must run before this step takes effect.
-- -----------------------------------------------------------------------------

-- Revoke mutating privileges from the application role.
-- The app role retains SELECT and INSERT only on this table.
REVOKE UPDATE, DELETE ON ledger_entries FROM confam_app;

-- Explicitly confirm the privileges the application role SHOULD retain.
-- This is documentation as much as enforcement — it states the intended grant
-- so a future audit can verify nothing has drifted.
-- (GRANT is a no-op if the role already has these; it will error if the role
--  does not exist — which is the intended behaviour: fail loudly, not silently.)
GRANT SELECT, INSERT ON ledger_entries TO confam_app;


-- -----------------------------------------------------------------------------
-- Step 4: Update the table comment to reflect the new enforcement mechanism.
-- -----------------------------------------------------------------------------

COMMENT ON TABLE ledger_entries IS
    'THE PRODUCT. Append-only naira-denominated record of every confirmed sale. '
    'UPDATE and DELETE are enforced by two complementary mechanisms: '
    '(1) REVOKE of UPDATE/DELETE from the application role confam_app (primary defence); '
    '(2) a BEFORE trigger that raises an exception for any role (second line of defence). '
    'The RULE blocks from migration 005 have been dropped — see migration 006 for rationale. '
    'Corrections are new rows (entry_type=correction) referencing the original via corrects_entry_id. '
    'See docs/DATA_MODEL.md §1, ENGINEERING_RULES.md Rule 2.';
