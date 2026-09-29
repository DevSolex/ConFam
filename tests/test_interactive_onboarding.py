"""
tests/test_interactive_onboarding.py

Tests for tap-to-select WhatsApp interactive messages:
  - REGISTER with no country suffix sends reply-buttons (not immediate registration)
  - REGISTER with explicit GHANA/NIGERIA suffix still works without buttons (typed path)
  - Button reply: valid register:* id creates merchant correctly
  - Button reply: malformed/unknown id handled gracefully, not a crash
  - List reply: valid bank:* id sets pending state and prompts for account number
  - List reply: bank:other routes to typed-fallback instructions
  - List reply: unknown id handled gracefully
  - Pending bank selection: account number arriving within TTL completes onboarding
  - Pending bank selection: account number arriving after expiry does NOT onboard
  - No pending selection: a 10-digit message is not misinterpreted as account number
  - Bare ONBOARD (no args) sends the interactive bank list
  - ONBOARD with args still works as before (typed fallback path)
  - COMMON_BANKS_BY_COUNTRY structure: 9 entries per country, no duplicates
  - encode/decode round-trips for both register and bank IDs
  - ID truncation when business name is very long
  - Ghana common-bank list includes mobile money entries (MTN, AirtelTigo, Vodafone)
"""

import json
import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch, patch as mock_patch

import pytest

from confam.interactive import (
    COMMON_BANKS_BY_COUNTRY,
    PENDING_BANK_SELECTION_TTL_SECONDS,
    CommonBank,
    clear_pending_bank_selection,
    decode_bank_id,
    decode_register_id,
    encode_bank_id,
    encode_register_id,
    get_pending_bank_selection,
    set_pending_bank_selection,
)

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

TEST_APP_SECRET = "test_interactive_secret"
BASE_URL = "http://test"


def _signed_text(from_id: str, text: str) -> tuple[bytes, dict]:
    """Build a signed text-message webhook payload."""
    payload = json.dumps({
        "entry": [{"changes": [{"value": {"messages": [
            {"from": from_id, "type": "text", "text": {"body": text}}
        ]}}]}]
    }).encode()
    sig = "sha256=" + hmac.new(
        TEST_APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return payload, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def _signed_button_reply(from_id: str, reply_id: str, title: str = "Button") -> tuple[bytes, dict]:
    """Build a signed button-reply interactive webhook payload."""
    payload = json.dumps({
        "entry": [{"changes": [{"value": {"messages": [
            {
                "from": from_id,
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": reply_id, "title": title},
                },
            }
        ]}}]}]
    }).encode()
    sig = "sha256=" + hmac.new(
        TEST_APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return payload, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def _signed_list_reply(from_id: str, reply_id: str, title: str = "Bank") -> tuple[bytes, dict]:
    """Build a signed list-reply interactive webhook payload."""
    payload = json.dumps({
        "entry": [{"changes": [{"value": {"messages": [
            {
                "from": from_id,
                "type": "interactive",
                "interactive": {
                    "type": "list_reply",
                    "list_reply": {"id": reply_id, "title": title},
                },
            }
        ]}}]}]
    }).encode()
    sig = "sha256=" + hmac.new(
        TEST_APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return payload, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def _last_whatsapp_call(mock_post) -> str:
    """Return the text body of the last WhatsApp send call, or '' if none."""
    if not mock_post.call_args_list:
        return ""
    call = mock_post.call_args_list[-1]
    j = call.kwargs.get("json") or (call.args[1] if len(call.args) > 1 else {})
    # Text message
    if j.get("type") == "text":
        return j.get("text", {}).get("body", "")
    # Interactive message — return stringified type for assertions
    if j.get("type") == "interactive":
        return f"[interactive:{j['interactive']['type']}]"
    return ""


def _last_interactive_payload(mock_post) -> dict:
    """Return the full json payload of the last call, or {}."""
    if not mock_post.call_args_list:
        return {}
    call = mock_post.call_args_list[-1]
    return call.kwargs.get("json") or {}


# ---------------------------------------------------------------------------
# ID encoding / decoding
# ---------------------------------------------------------------------------

class TestEncodeDecodeIds:

    def test_register_roundtrip_nigeria(self):
        encoded = encode_register_id("nigeria", "Adaeze Fashion Store")
        result = decode_register_id(encoded)
        assert result == ("nigeria", "Adaeze Fashion Store")

    def test_register_roundtrip_ghana(self):
        encoded = encode_register_id("ghana", "Kwame Textiles")
        result = decode_register_id(encoded)
        assert result == ("ghana", "Kwame Textiles")

    def test_register_id_truncated_long_name(self):
        """A 300-char business name must produce an id ≤ 256 chars."""
        long_name = "A" * 300
        encoded = encode_register_id("nigeria", long_name)
        assert len(encoded) <= 256
        result = decode_register_id(encoded)
        assert result is not None
        country, name = result
        assert country == "nigeria"
        assert len(name) < 300  # was truncated

    def test_register_decode_unknown_prefix_returns_none(self):
        assert decode_register_id("unknown:foo") is None

    def test_register_decode_missing_separator_returns_none(self):
        assert decode_register_id("register:NG") is None

    def test_register_decode_unknown_country_code_returns_none(self):
        assert decode_register_id("register:ZZ:Some Shop") is None

    def test_bank_roundtrip_nigerian_code(self):
        encoded = encode_bank_id("058")
        result = decode_bank_id(encoded)
        assert result == "058"

    def test_bank_roundtrip_ghana_mtn(self):
        encoded = encode_bank_id("MTN")
        result = decode_bank_id(encoded)
        assert result == "MTN"

    def test_bank_other_row_id(self):
        result = decode_bank_id("bank:other")
        assert result == "other"

    def test_bank_decode_wrong_prefix_returns_none(self):
        assert decode_bank_id("register:058") is None

    def test_bank_decode_empty_code_returns_none(self):
        assert decode_bank_id("bank:") is None


# ---------------------------------------------------------------------------
# COMMON_BANKS_BY_COUNTRY structure
# ---------------------------------------------------------------------------

class TestCommonBanksConfig:

    def test_nigeria_has_nine_or_fewer_entries(self):
        """Must leave room for the 'Other' row within Meta's 10-row limit."""
        assert len(COMMON_BANKS_BY_COUNTRY["nigeria"]) <= 9

    def test_ghana_has_nine_or_fewer_entries(self):
        assert len(COMMON_BANKS_BY_COUNTRY["ghana"]) <= 9

    def test_nigeria_no_duplicate_codes(self):
        codes = [b.code for b in COMMON_BANKS_BY_COUNTRY["nigeria"]]
        assert len(codes) == len(set(codes))

    def test_ghana_no_duplicate_codes(self):
        codes = [b.code for b in COMMON_BANKS_BY_COUNTRY["ghana"]]
        assert len(codes) == len(set(codes))

    def test_ghana_includes_mobile_money(self):
        """Ghana list must include at least one mobile money provider."""
        # MTN, AirtelTigo, or Vodafone — any one is sufficient
        mobile_money_codes = {"MTN", "ATL", "VOD"}
        ghana_codes = {b.code for b in COMMON_BANKS_BY_COUNTRY["ghana"]}
        assert mobile_money_codes & ghana_codes, (
            "Ghana common-bank list must include at least one mobile money provider "
            "(MTN, AirtelTigo, or Vodafone Cash). See confam/interactive.py."
        )

    def test_all_entries_are_common_bank_instances(self):
        for country, banks in COMMON_BANKS_BY_COUNTRY.items():
            for b in banks:
                assert isinstance(b, CommonBank), f"{country}: {b!r} is not a CommonBank"

    def test_bank_names_fit_meta_title_limit(self):
        """Meta list row title max 24 chars."""
        for country, banks in COMMON_BANKS_BY_COUNTRY.items():
            for b in banks:
                assert len(b.name) <= 24, (
                    f"{country}/{b.code}: name '{b.name}' exceeds 24 chars"
                )


# ---------------------------------------------------------------------------
# Pending bank selection DB helpers (unit — mocked conn)
# ---------------------------------------------------------------------------

class TestPendingBankSelectionHelpers:

    def _make_conn(self, bank_code=None, selected_at=None):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (bank_code, selected_at)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn

    def test_no_pending_selection_returns_none(self):
        conn = self._make_conn(None, None)
        result = get_pending_bank_selection(conn, "merchant-1")
        assert result is None

    def test_fresh_selection_returned(self):
        selected_at = datetime.now(UTC) - timedelta(seconds=60)  # 1 minute ago
        conn = self._make_conn("058", selected_at)
        result = get_pending_bank_selection(conn, "merchant-1")
        assert result is not None
        code, ts = result
        assert code == "058"

    def test_expired_selection_returns_none(self):
        """A selection older than TTL must not be returned."""
        old_ts = datetime.now(UTC) - timedelta(
            seconds=PENDING_BANK_SELECTION_TTL_SECONDS + 1
        )
        conn = self._make_conn("058", old_ts)
        result = get_pending_bank_selection(conn, "merchant-1")
        assert result is None

    def test_exactly_at_ttl_boundary_returns_none(self):
        """At exactly TTL seconds old it is expired (> not >=)."""
        ts = datetime.now(UTC) - timedelta(seconds=PENDING_BANK_SELECTION_TTL_SECONDS)
        conn = self._make_conn("058", ts)
        result = get_pending_bank_selection(conn, "merchant-1")
        assert result is None

    def test_set_pending_calls_update(self):
        conn = self._make_conn()
        cursor = conn.cursor.return_value.__enter__.return_value
        set_pending_bank_selection(conn, "merchant-1", "058")
        cursor.execute.assert_called_once()
        sql, params = cursor.execute.call_args[0]
        assert "pending_bank_code" in sql
        assert "058" in params

    def test_clear_pending_nulls_both_columns(self):
        conn = self._make_conn()
        cursor = conn.cursor.return_value.__enter__.return_value
        clear_pending_bank_selection(conn, "merchant-1")
        cursor.execute.assert_called_once()
        sql, _ = cursor.execute.call_args[0]
        assert "NULL" in sql
        assert "pending_bank_code" in sql
        assert "pending_bank_selected_at" in sql


# ---------------------------------------------------------------------------
# _is_plausible_account_number unit tests
# ---------------------------------------------------------------------------

class TestPlausibleAccountNumber:

    def _check(self, text: bool) -> bool:
        from services.messaging.main import _is_plausible_account_number
        return _is_plausible_account_number(text)

    def test_10_digits_is_plausible(self):
        assert self._check("0123456789") is True

    def test_10_digits_with_spaces_is_plausible(self):
        assert self._check("  0123456789  ") is True

    def test_9_digits_not_plausible(self):
        assert self._check("012345678") is False

    def test_11_digits_not_plausible(self):
        assert self._check("01234567890") is False

    def test_letters_not_plausible(self):
        assert self._check("PAY 500 Item") is False

    def test_empty_not_plausible(self):
        assert self._check("") is False

    def test_onboard_keyword_not_plausible(self):
        assert self._check("ONBOARD") is False


# ---------------------------------------------------------------------------
# Integration tests — webhook routing with mocked DB and Paystack
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_real_paystack():
    """Prevent any real Paystack HTTP call."""
    from confam import paystack
    def _boom(*a, **kw):
        raise AssertionError("real Paystack HTTP call in test")
    with patch.object(paystack.httpx, "get", _boom), \
         patch.object(paystack.httpx, "post", _boom):
        yield


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "12345")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("CHECKOUT_BASE_URL", "http://pay.confam.co")


class TestRegisterInteractiveButtons:
    """REGISTER with no country suffix → buttons; with suffix → direct register."""

    @pytest.mark.asyncio
    async def test_register_no_suffix_sends_buttons(self, env, monkeypatch):
        """REGISTER <name> with no suffix sends interactive buttons, does NOT register."""
        from httpx import ASGITransport, AsyncClient
        from services.messaging.main import app

        monkeypatch.setenv("DATABASE_URL", "")  # no DB needed for this path

        payload, headers = _signed_text("2348012345678", "REGISTER Adaeze Fashion Store")

        with (
            patch("services.messaging.main.resolve_merchant",
                  side_effect=__import__("services.messaging.main",
                                         fromlist=["UnregisteredSender"]).UnregisteredSender("x")),
            patch("services.messaging.main.send_register_country_buttons",
                  return_value=True) as mock_buttons,
            patch("services.messaging.main._handle_register_command") as mock_register,
            patch("services.messaging.main.get_conn") as mock_get_conn,
        ):
            mock_conn = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=BASE_URL
            ) as client:
                resp = await client.post(
                    "/webhooks/whatsapp", content=payload, headers=headers
                )

        assert resp.status_code == 200
        mock_buttons.assert_called_once_with("2348012345678", "Adaeze Fashion Store")
        mock_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_register_ghana_suffix_registers_directly(self, env, monkeypatch):
        """REGISTER <name> GHANA skips buttons and registers as Ghana immediately."""
        from httpx import ASGITransport, AsyncClient
        from services.messaging.main import app

        monkeypatch.setenv("DATABASE_URL", "")

        payload, headers = _signed_text("2348099999999", "REGISTER Kwame Textiles GHANA")

        with (
            patch("services.messaging.main.resolve_merchant",
                  side_effect=__import__("services.messaging.main",
                                         fromlist=["UnregisteredSender"]).UnregisteredSender("x")),
            patch("services.messaging.main.send_register_country_buttons") as mock_buttons,
            patch("services.messaging.main._handle_register_command",
                  return_value=__import__("fastapi.responses",
                                          fromlist=["Response"]).Response(status_code=200)
                  ) as mock_register,
            patch("services.messaging.main.get_conn") as mock_get_conn,
        ):
            mock_conn = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=BASE_URL
            ) as client:
                resp = await client.post(
                    "/webhooks/whatsapp", content=payload, headers=headers
                )

        assert resp.status_code == 200
        mock_buttons.assert_not_called()
        mock_register.assert_called_once_with(mock_conn, "2348099999999", "Kwame Textiles", "ghana")


class TestButtonReplyRouting:
    """Valid and invalid register button reply IDs."""

    @pytest.mark.asyncio
    async def test_valid_register_button_reply_creates_merchant(self, env, monkeypatch):
        """A valid register:NG:<name> button reply completes registration."""
        from httpx import ASGITransport, AsyncClient
        from services.messaging.main import app
        from fastapi import Response

        monkeypatch.setenv("DATABASE_URL", "")
        reply_id = encode_register_id("nigeria", "Test Shop")
        payload, headers = _signed_button_reply("2348011111111", reply_id)

        with (
            patch("services.messaging.main._handle_register_command",
                  return_value=Response(status_code=200)) as mock_register,
            patch("services.messaging.main.get_conn") as mock_get_conn,
        ):
            mock_conn = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=BASE_URL
            ) as client:
                resp = await client.post(
                    "/webhooks/whatsapp", content=payload, headers=headers
                )

        assert resp.status_code == 200
        mock_register.assert_called_once_with(mock_conn, "2348011111111", "Test Shop", "nigeria")

    @pytest.mark.asyncio
    async def test_malformed_button_reply_sends_graceful_message(self, env, monkeypatch):
        """A button reply with an unknown id does not crash — sends helpful reply."""
        from httpx import ASGITransport, AsyncClient
        from services.messaging.main import app

        monkeypatch.setenv("DATABASE_URL", "")
        payload, headers = _signed_button_reply("2348022222222", "garbage:id:xyz")

        with (
            patch("services.messaging.main._send_whatsapp") as mock_send,
            patch("services.messaging.main.get_conn") as mock_get_conn,
        ):
            mock_conn = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=BASE_URL
            ) as client:
                resp = await client.post(
                    "/webhooks/whatsapp", content=payload, headers=headers
                )

        assert resp.status_code == 200
        mock_send.assert_called_once()
        reply_text = mock_send.call_args[0][1]
        assert "REGISTER" in reply_text  # guidance, not a crash

    @pytest.mark.asyncio
    async def test_ghana_button_reply_creates_ghana_merchant(self, env, monkeypatch):
        """register:GH:<name> creates a Ghana merchant."""
        from httpx import ASGITransport, AsyncClient
        from services.messaging.main import app
        from fastapi import Response

        monkeypatch.setenv("DATABASE_URL", "")
        reply_id = encode_register_id("ghana", "Accra Fabrics")
        payload, headers = _signed_button_reply("233200000001", reply_id)

        with (
            patch("services.messaging.main._handle_register_command",
                  return_value=Response(status_code=200)) as mock_register,
            patch("services.messaging.main.get_conn") as mock_get_conn,
        ):
            mock_conn = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=BASE_URL
            ) as client:
                resp = await client.post(
                    "/webhooks/whatsapp", content=payload, headers=headers
                )

        assert resp.status_code == 200
        mock_register.assert_called_once_with(mock_conn, "233200000001", "Accra Fabrics", "ghana")


class TestListReplyRouting:
    """Bank list reply routing: tap a bank, tap Other, unknown id."""

    def _pending_merchant_conn(self, merchant_id="mid-1", country="nigeria"):
        """Mock conn for a pending_verification merchant."""
        conn = MagicMock()
        cursor = MagicMock()
        # resolve_merchant SELECT, then SELECT country
        cursor.fetchone.side_effect = [
            (merchant_id, "pending_verification"),  # resolve_merchant
            (country,),                              # SELECT country
        ]
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn

    @pytest.mark.asyncio
    async def test_valid_bank_tap_sets_pending_and_prompts_account(self, env, monkeypatch):
        """Tapping a bank row stores pending selection and asks for account number."""
        from httpx import ASGITransport, AsyncClient
        from services.messaging.main import app

        monkeypatch.setenv("DATABASE_URL", "")
        reply_id = encode_bank_id("058")
        payload, headers = _signed_list_reply("2348033333333", reply_id)

        with (
            patch("services.messaging.main.get_conn") as mock_get_conn,
            patch("services.messaging.main._send_whatsapp") as mock_send,
            patch("services.messaging.main.set_pending_bank_selection") as mock_set,
        ):
            mock_conn = self._pending_merchant_conn()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            # resolve_merchant must work
            with patch("services.messaging.main.resolve_merchant",
                       return_value={"merchant_id": "mid-1", "status": "pending_verification"}):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url=BASE_URL
                ) as client:
                    resp = await client.post(
                        "/webhooks/whatsapp", content=payload, headers=headers
                    )

        assert resp.status_code == 200
        mock_set.assert_called_once_with(mock_conn, "mid-1", "058")
        reply = mock_send.call_args[0][1]
        assert "account number" in reply.lower() or "10-digit" in reply.lower()

    @pytest.mark.asyncio
    async def test_bank_other_tap_sends_typed_fallback(self, env, monkeypatch):
        """Tapping 'Other' sends instructions for the typed ONBOARD flow."""
        from httpx import ASGITransport, AsyncClient
        from services.messaging.main import app

        monkeypatch.setenv("DATABASE_URL", "")
        payload, headers = _signed_list_reply("2348044444444", "bank:other")

        with (
            patch("services.messaging.main.get_conn") as mock_get_conn,
            patch("services.messaging.main._send_whatsapp") as mock_send,
        ):
            mock_conn = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=BASE_URL
            ) as client:
                resp = await client.post(
                    "/webhooks/whatsapp", content=payload, headers=headers
                )

        assert resp.status_code == 200
        mock_send.assert_called_once()
        reply = mock_send.call_args[0][1]
        assert "ONBOARD" in reply

    @pytest.mark.asyncio
    async def test_unknown_list_reply_id_handled_gracefully(self, env, monkeypatch):
        """An unrecognised list reply id sends a helpful message, not a crash."""
        from httpx import ASGITransport, AsyncClient
        from services.messaging.main import app

        monkeypatch.setenv("DATABASE_URL", "")
        payload, headers = _signed_list_reply("2348055555555", "completely:wrong:format")

        with (
            patch("services.messaging.main.get_conn") as mock_get_conn,
            patch("services.messaging.main._send_whatsapp") as mock_send,
        ):
            mock_conn = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=BASE_URL
            ) as client:
                resp = await client.post(
                    "/webhooks/whatsapp", content=payload, headers=headers
                )

        assert resp.status_code == 200
        mock_send.assert_called_once()
        reply = mock_send.call_args[0][1]
        assert "ONBOARD" in reply  # guidance present


class TestPendingBankSelectionFlow:
    """Account number arriving after bank tap — expiry and completion logic."""

    def _make_conn(self, pending_code=None, pending_ts=None, country="nigeria"):
        """Mock conn returning configurable pending state."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (pending_code, pending_ts)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn

    def test_account_number_within_ttl_completes_onboarding(self):
        """A 10-digit message while pending selection is active calls _handle_onboard_command."""
        from services.messaging.main import _route_pending
        from fastapi import Response

        fresh_ts = datetime.now(UTC) - timedelta(seconds=30)
        conn = self._make_conn("058", fresh_ts)

        with (
            patch("services.messaging.main.get_pending_bank_selection",
                  return_value=("058", fresh_ts)),
            patch("services.messaging.main.clear_pending_bank_selection"),
            patch("services.messaging.main._handle_onboard_command",
                  return_value=Response(status_code=200)) as mock_onboard,
        ):
            result = _route_pending(conn, "2348011111111", "mid-1", "0123456789", "0123456789"[:4])

        mock_onboard.assert_called_once()
        # Verify bank_code_already_resolved=True was passed
        call_kwargs = mock_onboard.call_args[1]
        assert call_kwargs.get("bank_code_already_resolved") is True

    def test_account_number_after_expiry_falls_through(self):
        """A 10-digit message after TTL expiry is NOT treated as completing onboarding."""
        from services.messaging.main import _route_pending
        from services.messaging.main import _send_whatsapp

        conn = self._make_conn()

        with (
            patch("services.messaging.main.get_pending_bank_selection", return_value=None),
            patch("services.messaging.main._handle_onboard_command") as mock_onboard,
            patch("services.messaging.main._send_whatsapp"),
            patch("services.messaging.main.send_onboard_bank_list", return_value=True),
        ):
            # "0123456789" looks like an account number but no pending selection
            # The _command_word("0123456789") == "0123456789" which is not "ONBOARD"
            # so it falls through to PENDING_HELP_MESSAGE
            _route_pending(conn, "2348099999999", "mid-1", "0123456789", "0")

        mock_onboard.assert_not_called()

    def test_bare_digit_message_with_no_pending_gets_help_message(self):
        """10-digit message with no pending selection → help, not onboarding."""
        from services.messaging.main import _route_pending

        conn = self._make_conn()

        with (
            patch("services.messaging.main.get_pending_bank_selection", return_value=None),
            patch("services.messaging.main._handle_onboard_command") as mock_onboard,
            patch("services.messaging.main._send_whatsapp") as mock_send,
            patch("services.messaging.main.send_onboard_bank_list", return_value=False),
        ):
            _route_pending(conn, "2348099999999", "mid-1", "0123456789", "0")

        mock_onboard.assert_not_called()
        # Some message was sent (help text)
        mock_send.assert_called()


class TestBareOnboardSendsListMessage:
    """Bare ONBOARD (no args) sends the interactive bank list."""

    def test_bare_onboard_calls_send_bank_list(self):
        from services.messaging.main import _route_pending

        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = ("nigeria",)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("services.messaging.main.get_pending_bank_selection", return_value=None),
            patch("services.messaging.main.send_onboard_bank_list",
                  return_value=True) as mock_list,
            patch("services.messaging.main._send_whatsapp"),
        ):
            _route_pending(conn, "2348011111111", "mid-1", "ONBOARD", "ONBOARD")

        mock_list.assert_called_once_with("2348011111111", "nigeria")

    def test_bare_onboard_falls_back_to_text_if_list_fails(self):
        """If the interactive list send fails, fall back to plain-text instructions."""
        from services.messaging.main import _route_pending

        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = ("nigeria",)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("services.messaging.main.get_pending_bank_selection", return_value=None),
            patch("services.messaging.main.send_onboard_bank_list", return_value=False),
            patch("services.messaging.main._send_whatsapp") as mock_send,
        ):
            _route_pending(conn, "2348011111111", "mid-1", "ONBOARD", "ONBOARD")

        mock_send.assert_called_once()
        reply = mock_send.call_args[0][1]
        assert "ONBOARD" in reply  # typed fallback instructions

    def test_onboard_with_args_still_works(self):
        """ONBOARD 0123456789 GTBank still takes the typed path."""
        from services.messaging.main import _route_pending
        from fastapi import Response

        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("services.messaging.main.get_pending_bank_selection", return_value=None),
            patch("services.messaging.main._handle_onboard_command",
                  return_value=Response(status_code=200)) as mock_onboard,
            patch("services.messaging.main.send_onboard_bank_list") as mock_list,
        ):
            _route_pending(
                conn, "2348011111111", "mid-1",
                "ONBOARD 0123456789 GTBank", "ONBOARD"
            )

        mock_list.assert_not_called()
        mock_onboard.assert_called_once()
        _, _, _, account, bank = mock_onboard.call_args[0]
        assert account == "0123456789"
        assert bank == "GTBank"


class TestGhanaMobileMoneyPath:
    """Ghana mobile money entries behave identically to bank entries in the tap flow.

    Mobile money codes (MTN/ATL/VOD) are treated as bank codes — same
    /bank/resolve + subaccount creation path. No code divergence.
    """

    def test_mtn_code_in_ghana_list(self):
        """MTN MoMo is in the Ghana common-bank list."""
        codes = {b.code for b in COMMON_BANKS_BY_COUNTRY["ghana"]}
        assert "MTN" in codes

    def test_mtn_bank_id_encode_decode(self):
        """MTN bank code round-trips through encode/decode like any bank code."""
        encoded = encode_bank_id("MTN")
        decoded = decode_bank_id(encoded)
        assert decoded == "MTN"

    def test_mobile_money_tap_sets_pending_with_mtn_code(self):
        """Tapping the MTN row stores 'MTN' as the pending bank code."""
        from services.messaging.main import _handle_list_reply
        from fastapi import Response

        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = ("ghana",)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("services.messaging.main.resolve_merchant",
                  return_value={"merchant_id": "mid-gh", "status": "pending_verification"}),
            patch("services.messaging.main.set_pending_bank_selection") as mock_set,
            patch("services.messaging.main._send_whatsapp"),
        ):
            result = _handle_list_reply(conn, "233200000001", encode_bank_id("MTN"))

        mock_set.assert_called_once_with(conn, "mid-gh", "MTN")

    def test_mobile_money_completes_onboarding_same_path_as_bank(self):
        """After tapping MTN + sending account number, onboarding completes normally."""
        from services.messaging.main import _route_pending
        from fastapi import Response
        from datetime import UTC, datetime, timedelta

        fresh_ts = datetime.now(UTC) - timedelta(seconds=30)
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("services.messaging.main.get_pending_bank_selection",
                  return_value=("MTN", fresh_ts)),
            patch("services.messaging.main.clear_pending_bank_selection"),
            patch("services.messaging.main._handle_onboard_command",
                  return_value=Response(status_code=200)) as mock_onboard,
        ):
            _route_pending(conn, "233200000001", "mid-gh", "0241234567", "0")

        mock_onboard.assert_called_once()
        call_args = mock_onboard.call_args
        # bank_name_raw should be the MTN code, bank_code_already_resolved=True
        assert call_args[0][4] == "MTN"
        assert call_args[1].get("bank_code_already_resolved") is True
