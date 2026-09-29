-- =============================================================================
-- Migration 014: add country to merchants
-- =============================================================================
-- Defaults to 'nigeria' for all existing rows — migration must not break
-- existing data. Every existing merchant is implicitly Nigerian.
--
-- Supported values for the pilot: 'nigeria', 'ghana'.
-- Kenya and Uganda are explicitly not supported yet — see OPEN_QUESTIONS.md
-- (OQ-Kenya, OQ-Uganda).

ALTER TABLE merchants
    ADD COLUMN IF NOT EXISTS country TEXT NOT NULL DEFAULT 'nigeria';

ALTER TABLE merchants
    ADD CONSTRAINT merchants_country_check
    CHECK (country IN ('nigeria', 'ghana'));

COMMENT ON COLUMN merchants.country IS
    'Country the merchant operates in. Determines which Paystack bank list, '
    'currency code (NGN/GHS), and account-resolution API to use. '
    'Default: nigeria. Existing rows are back-filled to nigeria automatically. '
    'See OPEN_QUESTIONS.md for Kenya/Uganda status.';
