-- =============================================================================
-- Migration: 013_flag_rail_event_disposition
--
-- Records what we DID with each rail event, not just whether it was processed.
--
-- WHY THIS IS NEEDED NOW
--   The buyer can now start several payment attempts on one link (card, then
--   bank transfer). A second charge.success for a link that is already 'logged'
--   means ConFam has been paid twice for one item and must refund the extra.
--   The old handler returned early and dropped the event on the floor: no
--   RailEvent, no alert, no trace. Ops had no way to find the surplus money.
--
--   rail_events.processed cannot carry this signal:
--     - processed = FALSE is the flag the reconciliation job polls for, so
--       leaving a duplicate FALSE would make reconciliation retry it forever.
--     - processed = TRUE makes it indistinguishable from a normally applied
--       sale. The only way to tell them apart is a LEFT JOIN against
--       ledger_entries, which cannot distinguish a duplicate from a payment
--       that arrived unroutable (no active payout account, subaccount
--       mismatch) — two problems with very different fixes.
--
-- THE VALUES
--   applied             — normal path. A sale LedgerEntry was written.
--   duplicate           — the link was already logged, so this is a second,
--                         distinct payment. NO LedgerEntry (Rule 2/11: never
--                         two sales for one link). Needs a manual refund.
--   applied_after_expiry — the link's expires_at had passed when the money
--                         arrived, but the sale was still recorded. Money in
--                         the door is never refused; this is a visibility flag
--                         only, not an incident.
--
-- GRANT NOTE (important — see commits a9be350 / dea9af3)
--   disposition is written at INSERT time, never by UPDATE. confam_app already
--   holds table-level INSERT on rail_events, so this migration needs NO new
--   privilege. That is deliberate: column-level UPDATE grants are exactly what
--   aborted the Render deploy twice, and adding one here would be a third
--   chance to crash a migration on a database whose column state we cannot
--   inspect from inside the revision.
--
--   The duplicate path therefore sets disposition AND processed in the same
--   INSERT: processed = TRUE because we have finished with the event (it was
--   reviewed and deliberately not applied), not because a sale was written.
--   The happy path still leaves processed FALSE at INSERT and flips it after
--   the ledger write commits, so a crash between the two is still recoverable
--   by reconciliation.
--
-- The 'unroutable' case is deliberately NOT a disposition value: those events
-- keep disposition = 'applied' with processed = FALSE, which is the existing
-- reconciliation signal and needs no new vocabulary.
-- =============================================================================

ALTER TABLE rail_events
    ADD COLUMN IF NOT EXISTS disposition TEXT NOT NULL DEFAULT 'applied';

ALTER TABLE rail_events
    DROP CONSTRAINT IF EXISTS rail_events_disposition_check;

ALTER TABLE rail_events
    ADD CONSTRAINT rail_events_disposition_check
    CHECK (disposition IN ('applied', 'duplicate', 'applied_after_expiry'));

COMMENT ON COLUMN rail_events.disposition IS
    'What we did with this rail event. ''applied'' = sale recorded. '
    '''duplicate'' = link was already logged, no sale written, refund manually. '
    '''applied_after_expiry'' = money arrived after the link expired, sale '
    'still recorded. Set at INSERT time; confam_app has no UPDATE on this column.';

-- Review queue for ops: duplicates and late payments, newest first.
CREATE INDEX IF NOT EXISTS rail_events_disposition_review_idx
    ON rail_events (disposition, received_at DESC)
    WHERE disposition <> 'applied';
