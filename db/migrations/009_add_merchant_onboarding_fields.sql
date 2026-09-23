-- =============================================================================
-- Migration: 009_add_merchant_onboarding_fields
--
-- Adds fields required for the merchant onboarding flow:
--   - business_name: used when creating the Paystack subaccount (required by
--     Paystack's Create Subaccount API).
--   - Also adds account_holder_name to payout_accounts: the name returned by
--     Paystack's account resolution API, stored for audit purposes
--     (COMPLIANCE_SECURITY.md §4 — verification tier can be strengthened later).
--
-- Both columns are nullable on existing rows; the application enforces them
-- as required on new inserts through the onboarding endpoint.
-- =============================================================================

ALTER TABLE merchants
    ADD COLUMN IF NOT EXISTS business_name TEXT;

COMMENT ON COLUMN merchants.business_name IS
    'Business trading name, as provided at onboarding. Required for Paystack '
    'subaccount creation. May differ from the bank account holder name — no '
    'automated matching at this verification tier (COMPLIANCE_SECURITY.md §4).';

ALTER TABLE payout_accounts
    ADD COLUMN IF NOT EXISTS account_holder_name TEXT;

COMMENT ON COLUMN payout_accounts.account_holder_name IS
    'Account holder name as returned by Paystack bank/resolve at verification time. '
    'Stored for audit trail. Not automatically matched against business_name at '
    'the current verification tier — this is a future KYB-depth feature '
    '(COMPLIANCE_SECURITY.md §4, verification_tier field on merchants).';
