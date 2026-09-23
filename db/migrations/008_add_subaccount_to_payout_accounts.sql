-- =============================================================================
-- Migration: 008_add_subaccount_to_payout_accounts
--
-- Adds the Paystack subaccount_code to payout_accounts.
-- This is the field the whitelist lookup returns when initializing a payment;
-- it is set once at merchant payout-account onboarding, same append-only
-- lifecycle as the rest of PayoutAccount (Rules 2 and 5).
--
-- No existing rows are modified — this is an additive change only.
-- Existing rows will have subaccount_code = NULL; the application treats
-- NULL as "bank rail not yet configured for this account."
-- =============================================================================

ALTER TABLE payout_accounts
    ADD COLUMN IF NOT EXISTS paystack_subaccount_code TEXT;

COMMENT ON COLUMN payout_accounts.paystack_subaccount_code IS
    'Paystack subaccount code (ACCT_xxxxxxxxxx) tied to the merchant''s bank account. '
    'Set at onboarding, never mutated — a change to the payout account creates a new row '
    'per Engineering Rules 2 and 5. NULL means bank rail is not yet configured for this account. '
    'This is the read-only value used by the settlement engine when initializing a Paystack '
    'transaction — it is never constructed at runtime (Engineering Rule 1).';
