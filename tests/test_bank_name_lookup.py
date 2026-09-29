"""
tests/test_bank_name_lookup.py

Tests for:
  - resolve_bank_name: exact match, close-typo match, ambiguous/no-match with candidates
  - list_banks: caching behaviour (cache hit doesn't call API again)
  - ONBOARD with bank name: Nigeria and Ghana (Paystack calls mocked, country param verified)
  - Existing merchants unaffected by migration (default country = 'nigeria')
  - Currency displayed correctly per merchant country in statement formatting
  - parse_register_command now returns (name, country) tuple
"""

import pathlib
import time
from unittest.mock import MagicMock, patch

import pytest

from confam.paystack import BankListEntry, BankNameNotResolved, resolve_bank_name


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

NGN_BANKS = [
    BankListEntry(name="Guaranty Trust Bank", code="058", country="nigeria"),
    BankListEntry(name="Access Bank", code="044", country="nigeria"),
    BankListEntry(name="First Bank of Nigeria", code="011", country="nigeria"),
    BankListEntry(name="United Bank for Africa", code="033", country="nigeria"),
    BankListEntry(name="Zenith Bank", code="057", country="nigeria"),
    BankListEntry(name="Sterling Bank", code="232", country="nigeria"),
    BankListEntry(name="Polaris Bank", code="076", country="nigeria"),
]

GHS_BANKS = [
    BankListEntry(name="Ghana Commercial Bank", code="GCB", country="ghana"),
    BankListEntry(name="Fidelity Bank Ghana", code="FBG", country="ghana"),
    BankListEntry(name="Stanbic Bank Ghana", code="SBG", country="ghana"),
    BankListEntry(name="Ecobank Ghana", code="ECO", country="ghana"),
]


# ---------------------------------------------------------------------------
# Bank-name fuzzy matching
# ---------------------------------------------------------------------------

class TestResolveBankName:

    def _patch_list(self, country, banks):
        return patch("confam.paystack.list_banks", return_value=banks)

    # --- exact matches ---

    def test_exact_match_full_name(self):
        with self._patch_list("nigeria", NGN_BANKS):
            code, name = resolve_bank_name("Guaranty Trust Bank", "nigeria")
        assert code == "058"
        assert name == "Guaranty Trust Bank"

    def test_exact_match_case_insensitive(self):
        with self._patch_list("nigeria", NGN_BANKS):
            code, _ = resolve_bank_name("guaranty trust bank", "nigeria")
        assert code == "058"

    def test_noise_word_stripped_plc(self):
        """'GTBank PLC' — strips 'plc', matches 'Guaranty Trust Bank'."""
        with self._patch_list("nigeria", NGN_BANKS):
            code, _ = resolve_bank_name("GTBank PLC", "nigeria")
        assert code == "058"

    # --- substring matches ---

    def test_short_name_substring_match(self):
        """'Zenith' is a substring of 'Zenith Bank' after noise-stripping."""
        with self._patch_list("nigeria", NGN_BANKS):
            code, _ = resolve_bank_name("Zenith", "nigeria")
        assert code == "057"

    def test_gtbank_shorthand(self):
        """'GTBank' should match 'Guaranty Trust Bank' via substring logic."""
        with self._patch_list("nigeria", NGN_BANKS):
            code, _ = resolve_bank_name("GTBank", "nigeria")
        assert code == "058"

    def test_access_bank_partial(self):
        """'Access' matches 'Access Bank'."""
        with self._patch_list("nigeria", NGN_BANKS):
            code, _ = resolve_bank_name("Access", "nigeria")
        assert code == "044"

    # --- close-typo (fuzzy ratio) matches ---

    def test_close_typo_sterling(self):
        """'Steling Bank' (transposition) should still resolve to Sterling Bank."""
        with self._patch_list("nigeria", NGN_BANKS):
            code, _ = resolve_bank_name("Steling Bank", "nigeria")
        assert code == "232"

    def test_close_typo_polaris(self):
        """'Polaris Bnk' should still resolve to Polaris Bank."""
        with self._patch_list("nigeria", NGN_BANKS):
            code, _ = resolve_bank_name("Polaris Bnk", "nigeria")
        assert code == "076"

    # --- ambiguous / no-match ---

    def test_no_match_raises_bank_name_not_resolved(self):
        """'Completely Unknown XYZ Bank' raises BankNameNotResolved."""
        with self._patch_list("nigeria", NGN_BANKS):
            with pytest.raises(BankNameNotResolved) as exc_info:
                resolve_bank_name("Completely Unknown XYZ", "nigeria")
        assert len(exc_info.value.candidates) <= 3

    def test_no_match_returns_candidates(self):
        """BankNameNotResolved carries non-empty candidates list."""
        with self._patch_list("nigeria", NGN_BANKS):
            with pytest.raises(BankNameNotResolved) as exc_info:
                resolve_bank_name("ZZZ Unknown Bank", "nigeria")
        assert isinstance(exc_info.value.candidates, list)
        assert len(exc_info.value.candidates) >= 1  # always returns up to 3

    def test_very_short_input_raises(self):
        """A 2-char input must not match (too short to be confident)."""
        with self._patch_list("nigeria", NGN_BANKS):
            with pytest.raises(BankNameNotResolved):
                resolve_bank_name("AB", "nigeria")

    def test_empty_normalised_input_raises(self):
        """Input that normalises to empty (e.g. all noise words) raises."""
        with self._patch_list("nigeria", NGN_BANKS):
            with pytest.raises(BankNameNotResolved):
                resolve_bank_name("bank plc ltd", "nigeria")

    # --- Ghana ---

    def test_ghana_bank_list_used_for_ghana(self):
        """Ghana country uses Ghana bank list — 'Ghana Commercial' resolves."""
        with self._patch_list("ghana", GHS_BANKS):
            code, name = resolve_bank_name("Ghana Commercial", "ghana")
        assert code == "GCB"
        assert "Ghana Commercial" in name

    def test_ghana_ecobank_match(self):
        with self._patch_list("ghana", GHS_BANKS):
            code, _ = resolve_bank_name("Ecobank", "ghana")
        assert code == "ECO"

    def test_ghana_fidelity_match(self):
        with self._patch_list("ghana", GHS_BANKS):
            code, _ = resolve_bank_name("Fidelity", "ghana")
        assert code == "FBG"


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------

class TestListBanksCache:

    def test_cache_hit_does_not_call_api_again(self):
        """Two calls within TTL make only one HTTP request."""
        import confam.paystack as ps

        api_banks = [
            {"name": "Cache Test Bank", "code": "CTB", "country": "nigeria", "active": True}
        ]
        api_response = {"status": True, "data": api_banks}

        # Clear any prior cache for nigeria
        ps._BANK_LIST_CACHE.pop("nigeria", None)

        with (
            patch.dict("os.environ", {"PAYSTACK_SECRET_KEY": "test_key"}),
            patch("confam.paystack.httpx") as mock_httpx,
        ):
            mock_resp = MagicMock()
            mock_resp.json.return_value = api_response
            mock_resp.raise_for_status = MagicMock()
            mock_httpx.get.return_value = mock_resp

            r1 = ps.list_banks("nigeria")
            r2 = ps.list_banks("nigeria")  # cache hit

        assert mock_httpx.get.call_count == 1
        assert r1 == r2
        assert len(r1) == 1
        assert r1[0].code == "CTB"

        # Restore clean state
        ps._BANK_LIST_CACHE.pop("nigeria", None)

    def test_expired_cache_calls_api_again(self):
        """After TTL expiry, the next call fetches from API."""
        import confam.paystack as ps

        api_banks = [
            {"name": "Fresh Bank", "code": "FRB", "country": "nigeria", "active": True}
        ]
        api_response = {"status": True, "data": api_banks}

        # Plant a stale entry (2 hours ago)
        ps._BANK_LIST_CACHE["nigeria"] = (time.monotonic() - 7200, [])

        with (
            patch.dict("os.environ", {"PAYSTACK_SECRET_KEY": "test_key"}),
            patch("confam.paystack.httpx") as mock_httpx,
        ):
            mock_resp = MagicMock()
            mock_resp.json.return_value = api_response
            mock_resp.raise_for_status = MagicMock()
            mock_httpx.get.return_value = mock_resp

            result = ps.list_banks("nigeria")

        assert mock_httpx.get.call_count == 1
        assert len(result) == 1
        assert result[0].code == "FRB"

        ps._BANK_LIST_CACHE.pop("nigeria", None)

    def test_ghana_and_nigeria_cached_independently(self):
        """Ghana and Nigeria have separate cache entries."""
        import confam.paystack as ps

        ps._BANK_LIST_CACHE.pop("nigeria", None)
        ps._BANK_LIST_CACHE.pop("ghana", None)

        def make_response(country_code):
            resp = MagicMock()
            resp.json.return_value = {
                "status": True,
                "data": [{"name": f"{country_code} Bank", "code": country_code, "country": country_code, "active": True}],
            }
            resp.raise_for_status = MagicMock()
            return resp

        with (
            patch.dict("os.environ", {"PAYSTACK_SECRET_KEY": "test_key"}),
            patch("confam.paystack.httpx") as mock_httpx,
        ):
            mock_httpx.get.side_effect = [
                make_response("nigeria"),
                make_response("ghana"),
            ]
            ngn = ps.list_banks("nigeria")
            ghs = ps.list_banks("ghana")
            # Second calls — cache hits
            ngn2 = ps.list_banks("nigeria")
            ghs2 = ps.list_banks("ghana")

        assert mock_httpx.get.call_count == 2  # one miss per country
        assert ngn[0].code == "nigeria"
        assert ghs[0].code == "ghana"
        assert ngn == ngn2
        assert ghs == ghs2

        ps._BANK_LIST_CACHE.pop("nigeria", None)
        ps._BANK_LIST_CACHE.pop("ghana", None)


# ---------------------------------------------------------------------------
# ONBOARD with bank name — country param forwarded correctly
# ---------------------------------------------------------------------------

class TestOnboardBankNameCountryParam:
    """
    Verify that _handle_onboard_command passes the merchant's country to
    list_banks so the correct bank list is queried.
    These tests mock all Paystack and DB calls.
    """

    def _make_mock_conn(self, country: str, business_name: str = "Test Biz"):
        """Return a mock psycopg2 connection simulating a pending merchant."""
        from confam.paystack import ResolvedAccount, CreatedSubaccount

        conn = MagicMock()
        cursor = MagicMock()
        # fetchone calls in order:
        # 1. get_active_payout_account -> NoActivePayoutAccount (raises, not fetchone)
        # 2. SELECT country, business_name FROM merchants
        # 3. INSERT payout_accounts RETURNING / UPDATE merchants (no fetchone needed for these)
        cursor.fetchone.return_value = (country, business_name)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn

    def test_nigeria_merchant_uses_nigeria_bank_list(self):
        from services.messaging.main import _handle_onboard_command
        from confam.paystack import ResolvedAccount, CreatedSubaccount
        from confam.payout_accounts import NoActivePayoutAccount

        conn = self._make_mock_conn("nigeria")
        ngn_banks = [BankListEntry(name="Guaranty Trust Bank", code="058", country="nigeria")]

        with (
            patch("confam.payout_accounts.get_active_payout_account",
                  side_effect=NoActivePayoutAccount()),
            patch("services.messaging.main.check_resolve_rate_limit", return_value=False),
            patch("confam.paystack.list_banks", return_value=ngn_banks) as mock_list,
            patch("confam.paystack.resolve_bank_account",
                  return_value=ResolvedAccount("0123456789", "Test Merchant", 9)),
            patch("confam.paystack.create_subaccount",
                  return_value=CreatedSubaccount("ACCT_test", "Test Biz")),
            patch("services.messaging.main._send_whatsapp"),
        ):
            _handle_onboard_command(conn, "2348012345678", "mid-ng", "0123456789", "GTBank")
            mock_list.assert_called_once_with("nigeria")

    def test_ghana_merchant_uses_ghana_bank_list(self):
        from services.messaging.main import _handle_onboard_command
        from confam.paystack import ResolvedAccount, CreatedSubaccount
        from confam.payout_accounts import NoActivePayoutAccount

        conn = self._make_mock_conn("ghana", "Kwame Textiles")
        ghs_banks = [BankListEntry(name="Ghana Commercial Bank", code="GCB", country="ghana")]

        with (
            patch("confam.payout_accounts.get_active_payout_account",
                  side_effect=NoActivePayoutAccount()),
            patch("services.messaging.main.check_resolve_rate_limit", return_value=False),
            patch("confam.paystack.list_banks", return_value=ghs_banks) as mock_list,
            patch("confam.paystack.resolve_bank_account",
                  return_value=ResolvedAccount("0123456789", "Kwame Merchant", 1)),
            patch("confam.paystack.create_subaccount",
                  return_value=CreatedSubaccount("ACCT_ghs", "Kwame Textiles")),
            patch("services.messaging.main._send_whatsapp"),
        ):
            _handle_onboard_command(
                conn, "233012345678", "mid-gh", "0123456789", "Ghana Commercial"
            )
            mock_list.assert_called_once_with("ghana")

    def test_bank_name_not_resolved_sends_candidates(self):
        """When name resolution fails, merchant gets up to 3 candidate names."""
        from services.messaging.main import _handle_onboard_command
        from confam.payout_accounts import NoActivePayoutAccount

        conn = self._make_mock_conn("nigeria")
        candidates = [
            BankListEntry("Guaranty Trust Bank", "058", "nigeria"),
            BankListEntry("Access Bank", "044", "nigeria"),
            BankListEntry("Zenith Bank", "057", "nigeria"),
        ]

        with (
            patch("confam.payout_accounts.get_active_payout_account",
                  side_effect=NoActivePayoutAccount()),
            patch("services.messaging.main.check_resolve_rate_limit", return_value=False),
            patch("confam.paystack.resolve_bank_name",
                  side_effect=BankNameNotResolved("not found", candidates)),
            patch("services.messaging.main._send_whatsapp") as mock_send,
        ):
            _handle_onboard_command(conn, "2348012345678", "mid-ng", "0123456789", "XYZ Unknown")

        # The WhatsApp reply should contain candidate bank names
        sent_text = mock_send.call_args[0][1]
        assert "Guaranty Trust Bank" in sent_text or "Did you mean" in sent_text


# ---------------------------------------------------------------------------
# Existing merchants unaffected by country migration
# ---------------------------------------------------------------------------

class TestExistingMerchantsUnaffected:

    def test_migration_sql_has_correct_default(self):
        """Migration 014 sets DEFAULT 'nigeria' — existing rows back-filled."""
        sql = pathlib.Path(
            "/home/solex/Desktop/ConFam/db/migrations/014_add_merchant_country.sql"
        ).read_text()
        assert "DEFAULT 'nigeria'" in sql

    def test_migration_sql_has_check_constraint(self):
        sql = pathlib.Path(
            "/home/solex/Desktop/ConFam/db/migrations/014_add_merchant_country.sql"
        ).read_text()
        assert "'nigeria'" in sql
        assert "'ghana'" in sql

    def test_parse_register_returns_nigeria_by_default(self):
        from services.messaging.main import parse_register_command
        name, country = parse_register_command("REGISTER Adaeze Fashion Store")
        assert name == "Adaeze Fashion Store"
        assert country == "nigeria"

    def test_parse_register_ghana_suffix(self):
        from services.messaging.main import parse_register_command
        name, country = parse_register_command("REGISTER Kwame Textiles GHANA")
        assert name == "Kwame Textiles"
        assert country == "ghana"

    def test_parse_register_ghana_case_insensitive(self):
        from services.messaging.main import parse_register_command
        name, country = parse_register_command("REGISTER Kwame Textiles ghana")
        assert name == "Kwame Textiles"
        assert country == "ghana"

    def test_parse_register_ghana_only_suffix_errors(self):
        """'REGISTER GHANA' (no business name) raises ParseError."""
        from services.messaging.main import parse_register_command, ParseError
        with pytest.raises(ParseError):
            parse_register_command("REGISTER GHANA")

    def test_parse_register_returns_tuple(self):
        """parse_register_command now returns a 2-tuple, not a string."""
        from services.messaging.main import parse_register_command
        result = parse_register_command("REGISTER My Store")
        assert isinstance(result, tuple)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Currency display — format_amount
# ---------------------------------------------------------------------------

class TestCurrencyDisplay:

    def test_format_amount_ngn(self):
        from services.messaging.statement import format_amount
        assert format_amount(250000, "NGN") == "NGN 2,500.00"

    def test_format_amount_ghs(self):
        from services.messaging.statement import format_amount
        assert format_amount(250000, "GHS") == "GHS 2,500.00"

    def test_format_amount_default_ngn(self):
        from services.messaging.statement import format_amount
        assert format_amount(100) == "NGN 1.00"

    def test_format_naira_alias_still_works(self):
        """Backward-compat: format_naira is an alias for format_amount."""
        from services.messaging.statement import format_naira
        assert format_naira(100) == "NGN 1.00"

    def test_format_amount_large_value(self):
        from services.messaging.statement import format_amount
        # 1,234,567 kobo = NGN 12,345.67
        assert format_amount(1234567, "NGN") == "NGN 12,345.67"

    def test_format_amount_rejects_float(self):
        from services.messaging.statement import format_amount
        with pytest.raises(TypeError):
            format_amount(250.0, "NGN")

    def test_ledger_row_currency_field_defaults_ngn(self):
        """LedgerRow has a currency field defaulting to 'NGN'."""
        from datetime import datetime, UTC
        from services.messaging.statement import LedgerRow
        row = LedgerRow(
            confirmed_at=datetime.now(UTC),
            description="test",
            amount_minor_units=5000,
            status="Confirmed",
            entry_type="sale",
            rail="bank",
        )
        assert row.currency == "NGN"

    def test_ledger_row_currency_ghs(self):
        from datetime import datetime, UTC
        from services.messaging.statement import LedgerRow
        row = LedgerRow(
            confirmed_at=datetime.now(UTC),
            description="test",
            amount_minor_units=5000,
            status="Confirmed",
            entry_type="sale",
            rail="bank",
            currency="GHS",
        )
        assert row.currency == "GHS"
