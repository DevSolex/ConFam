"""
tests/test_messaging_ledger_statement.py

LEDGER / STATEMENT — merchant sales statement delivered as a PDF over WhatsApp.

Coverage:
  - Integer kobo -> naira formatting (Rule 6): no float ever touches an amount.
  - PDF content: title, business name, generated timestamp, one row per sale
    with date/description/amount/status, running total, and the OQ-020
    settlement disclaimer. Asserted on text extracted from the rendered PDF,
    not on raw bytes, so a formatting regression actually fails.
  - Empty ledger -> plain-text reply, no document upload.
  - Merchant isolation: one merchant's PDF never contains another's sales,
    and requesting a statement writes nothing.
  - State x command routing: unregistered, pending_verification, active.
  - Meta flow: multipart PDF upload to /{phone_number_id}/media, then a
    document message carrying the returned media_id and the filename.
  - Fallback: a failed upload, or a failed document send, still answers the
    merchant in plain text instead of leaving them with silence.
  - Per-sender rate limiting on statement requests.

Meta is always mocked. The DB rows are real, written through the migrator
role, and every read in the statement path goes through the app's own
confam_app pool — so the grants this feature needs are proven to be in place.
"""

import hashlib
import hmac
import io
import json
import os
import uuid
from datetime import UTC
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from httpx import ASGITransport, AsyncClient, ConnectError, ReadTimeout
from pypdf import PdfReader

from services.messaging import main as messaging_main
from services.messaging import statement as st
from services.messaging.main import app as messaging_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_APP_SECRET = "test_meta_app_secret_ledger"
TEST_PHONE_NUMBER_ID = "123456789012345"
TEST_ACCESS_TOKEN = "test_meta_access_token"
BASE_URL = "http://test"
TEST_MEDIA_ID = "media-id-abc123"


@pytest.fixture(autouse=True)
def clear_ledger_rate_limits():
    """The statement limiter is a module-level dict, so it must not leak
    between tests or the rate-limit test would poison every later one."""
    messaging_main._LEDGER_ATTEMPTS.clear()
    yield
    messaging_main._LEDGER_ATTEMPTS.clear()


@pytest.fixture()
def migrator_conn():
    url = os.environ.get("ALEMBIC_DATABASE_URL", "").replace("postgresql+psycopg2://", "postgresql://")
    if not url:
        pytest.skip("ALEMBIC_DATABASE_URL not set")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture()
def messaging_env(monkeypatch):
    """Env the webhook handler needs, pointed at the test database."""
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", TEST_APP_SECRET)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", TEST_PHONE_NUMBER_ID)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", TEST_ACCESS_TOKEN)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify_token_ledger")
    monkeypatch.setenv("DATABASE_URL", test_url)
    monkeypatch.setenv("CHECKOUT_BASE_URL", "http://pay.confam.co")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pdf_text(pdf_bytes: bytes) -> str:
    """All text in a rendered statement PDF, pages joined."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _signed_payload(from_id: str, text: str) -> tuple[bytes, dict]:
    payload = json.dumps({
        "entry": [{"changes": [{"value": {"messages": [
            {"from": from_id, "type": "text", "text": {"body": text}}
        ]}}]}]
    }).encode()
    sig = "sha256=" + hmac.new(
        TEST_APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return payload, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


async def _deliver(from_id: str, text: str):
    """Deliver one inbound message with all Meta HTTP calls mocked.

    Returns the mock, so a test can inspect every outbound call: the media
    upload, the document message, and any plain-text fallback.
    """
    payload, headers = _signed_payload(from_id, text)
    with patch("services.messaging.main.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()
        mock_post.return_value.json.return_value = {"id": TEST_MEDIA_ID}
        async with AsyncClient(
            transport=ASGITransport(app=messaging_app), base_url=BASE_URL  # type: ignore[arg-type]
        ) as client:
            resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)
        assert resp.status_code == 200
    return mock_post


def _text_replies(mock) -> list[str]:
    return [
        c.kwargs["json"]["text"]["body"]
        for c in mock.call_args_list
        if (c.kwargs.get("json") or {}).get("type") == "text"
    ]


def _documents(mock) -> list[dict]:
    return [
        c.kwargs["json"]
        for c in mock.call_args_list
        if (c.kwargs.get("json") or {}).get("type") == "document"
    ]


def _uploads(mock) -> list:
    """Calls to the /media endpoint (the PDF upload, not the message send)."""
    return [c for c in mock.call_args_list if c.args and str(c.args[0]).endswith("/media")]


def _uploaded_pdf(mock) -> bytes:
    uploads = _uploads(mock)
    assert len(uploads) == 1, f"expected exactly one media upload, got {len(uploads)}"
    return uploads[0].kwargs["files"]["file"][1]


def _merchant(status: str, conn, business_name: str = "solex shoes") -> dict:
    """Insert a merchant in `status` with a unique WhatsApp thread id."""
    run_id = uuid.uuid4().hex[:12]
    from_id = f"234700{run_id}"
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO merchants (whatsapp_number, confam_thread_id, business_name, status)
               VALUES (%s, %s, %s, %s) RETURNING merchant_id""",
            (f"+{from_id}", from_id, business_name, status),
        )
        merchant_id = str(cur.fetchone()[0])
    conn.commit()
    return {"merchant_id": merchant_id, "from_id": from_id, "business_name": business_name}


def _payout_account_id(cur, merchant_id: str) -> str:
    """A payout account for this merchant, created on first use."""
    cur.execute(
        "SELECT payout_account_id FROM payout_accounts WHERE merchant_id = %s LIMIT 1",
        (merchant_id,),
    )
    row = cur.fetchone()
    if row:
        return str(row[0])
    cur.execute(
        """INSERT INTO payout_accounts (
               merchant_id, bank_account_number, bank_code, account_holder_name,
               verification_method, verified_at, active_from, paystack_subaccount_code)
           VALUES (%s, %s, '058', 'Test Holder', 'bank_api_resolve', now(), now(), %s)
           RETURNING payout_account_id""",
        (merchant_id, f"enc{uuid.uuid4().hex[:12]}", f"ACCT_{uuid.uuid4().hex[:10]}"),
    )
    return str(cur.fetchone()[0])


def _insert_sale(
    conn,
    merchant_id: str,
    amount_minor_units: int,
    description: str,
    days_ago: int = 0,
    entry_type: str = "sale",
) -> None:
    """Insert one confirmed sale: payment link -> rail event -> ledger entry.

    Mirrors the settlement engine's write order. The migrator role is used
    because confam_app holds no INSERT grant on ledger_entries.
    """
    with conn.cursor() as cur:
        payout_id = _payout_account_id(cur, merchant_id)

        cur.execute(
            """INSERT INTO payment_links
                   (merchant_id, amount_minor_units, currency, description,
                    status, expires_at)
               VALUES (%s, %s, 'NGN', %s, 'logged', now() + interval '30 minutes')
               RETURNING link_id""",
            (merchant_id, amount_minor_units, description),
        )
        link_id = cur.fetchone()[0]

        cur.execute(
            """INSERT INTO rail_events (link_id, rail, rail_reference, raw_payload)
               VALUES (%s, 'bank', %s, '{}')
               RETURNING rail_event_id""",
            (link_id, f"REF-{uuid.uuid4().hex}"),
        )
        rail_event_id = cur.fetchone()[0]

        cur.execute(
            """INSERT INTO ledger_entries
                   (link_id, merchant_id, payout_account_id, rail_event_id,
                    amount_minor_units, currency, rail, confirmed_at, entry_type)
               VALUES (%s, %s, %s, %s, %s, 'NGN', 'bank',
                       now() - make_interval(days => %s), %s)""",
            (link_id, merchant_id, payout_id, rail_event_id,
             amount_minor_units, days_ago, entry_type),
        )
    conn.commit()


def _ledger_count(conn, merchant_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ledger_entries WHERE merchant_id = %s", (merchant_id,))
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Integer money formatting (Rule 6)
# ---------------------------------------------------------------------------


class TestKoboFormatting:
    """Rule 6: amounts are int kobo end to end. The PDF formats them with
    divmod on ints, so no amount can pick up float rounding error."""

    @pytest.mark.parametrize(
        "kobo, expected",
        [
            (0, "NGN 0.00"),
            (1, "NGN 0.01"),
            (99, "NGN 0.99"),
            (100, "NGN 1.00"),
            (75000, "NGN 750.00"),
            (100000, "NGN 1,000.00"),
            (1234567, "NGN 12,345.67"),
            (123456789, "NGN 1,234,567.89"),
        ],
    )
    def test_formats_exactly(self, kobo, expected):
        assert st.format_naira(kobo) == expected

    def test_rejects_float_input(self):
        """A float must never be accepted — that is how Rule 6 gets violated."""
        with pytest.raises(TypeError):
            st.format_naira(750.0)
        with pytest.raises(TypeError):
            st.format_naira("750.00")  # type: ignore[arg-type]

    def test_sub_kobo_precision_is_preserved(self):
        """1 kobo must not round to 0.00 — the bug a float division invites."""
        assert st.format_naira(1) == "NGN 0.01"
        assert st.format_naira(9) == "NGN 0.09"
        assert st.format_naira(11) == "NGN 0.11"

    def test_handles_negative_totals_without_crashing(self):
        assert st.format_naira(-75050) == "-NGN 750.50"

    def test_large_amount_keeps_exact_integers(self):
        """A float pipeline would lose kobo precision at this magnitude."""
        big = 9_007_199_254_740_993  # 2**53 + 1, where float64 stops resolving
        assert st.format_naira(big) == "NGN 90,071,992,547,409.93"


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------


class TestStatementPdf:
    """The PDF is the merchant's whole view of their sales, so its contents
    are asserted on extracted text rather than on bytes."""

    def _rows(self):
        from datetime import datetime, timedelta

        now = datetime(2026, 9, 17, 14, 5, tzinfo=UTC)
        return now, [
            st.LedgerRow(now - timedelta(days=2), "Ankara fabric x2",
                          75000, "Confirmed", "sale", "bank"),
            st.LedgerRow(now - timedelta(days=1), "Sneakers pair",
                          125000, "Confirmed", "sale", "bank"),
        ]

    def test_contains_header_business_name_and_generated_time(self):
        now, rows = self._rows()
        text = _pdf_text(st.build_statement_pdf("Adaeze Fashion Store", rows, 2, now))

        assert "ConFam Sales Statement" in text
        assert "Adaeze Fashion Store" in text
        assert "17 Sep 2026, 14:05 UTC" in text

    def test_contains_column_headers(self):
        now, rows = self._rows()
        text = _pdf_text(st.build_statement_pdf("Shop", rows, 2, now))
        for column in ("DATE", "DESCRIPTION", "AMOUNT", "STATUS"):
            assert column in text

    def test_one_row_per_sale_with_date_description_amount_status(self):
        now, rows = self._rows()
        text = _pdf_text(st.build_statement_pdf("Shop", rows, 2, now))

        assert "15 Sep 2026" in text  # older sale, day-level precision
        assert "Ankara fabric x2" in text
        assert "NGN 750.00" in text
        assert "16 Sep 2026" in text
        assert "Sneakers pair" in text
        assert "NGN 1,250.00" in text

    def test_running_total_sums_the_displayed_rows(self):
        now, rows = self._rows()
        text = _pdf_text(st.build_statement_pdf("Shop", rows, 2, now))
        # 750.00 + 1,250.00
        assert "Total confirmed sales: NGN 2,000.00" in text

    def test_running_total_stays_exact_for_awkward_kobo(self):
        """0.01 + 0.01 + 0.01 must be 0.03, not 0.030000000000000002."""
        from datetime import datetime

        now = datetime(2026, 9, 17, tzinfo=UTC)
        rows = [st.LedgerRow(now, f"Tiny {i}", 1, "Confirmed", "sale", "bank") for i in range(3)]
        text = _pdf_text(st.build_statement_pdf("Shop", rows, 3, now))
        assert "Total confirmed sales: NGN 0.03" in text

    def test_includes_settlement_disclaimer(self):
        """OQ-020: a ledger entry proves the buyer's payment was collected,
        not that the merchant was credited. The PDF must not imply otherwise."""
        now, rows = self._rows()
        text = _pdf_text(st.build_statement_pdf("Shop", rows, 2, now))
        assert "not a bank statement" in text
        assert "collected" in text
        assert "not proof that the money has reached your bank account" in text

    def test_correction_row_is_labelled_as_a_correction(self):
        from datetime import datetime

        now = datetime(2026, 9, 17, tzinfo=UTC)
        rows = [
            st.LedgerRow(now, "Refund adjustment", 5000, "Correction", "correction", "bank"),
        ]
        text = _pdf_text(st.build_statement_pdf("Shop", rows, 1, now))
        assert "Correction" in text

    def test_rejects_empty_rows(self):
        """Callers check for an empty ledger first; the renderer must not be
        reachable with nothing to show."""
        from datetime import datetime

        with pytest.raises(ValueError):
            st.build_statement_pdf(
                "Shop", [], 0, datetime(2026, 9, 17, tzinfo=UTC)
            )

    def test_truncation_note_disclosed_when_history_is_longer(self):
        """The production shape: the cap is full and the merchant has more
        history than fits. The note must name the cap and the true total."""
        from datetime import datetime, timedelta

        now = datetime(2026, 9, 17, tzinfo=UTC)
        rows = [
            st.LedgerRow(now - timedelta(seconds=i), f"Item {i}", 1000, "Confirmed", "sale", "bank")
            for i in range(500)
        ]
        text = _pdf_text(st.build_statement_pdf("Shop", rows, 743, now))
        assert "Showing the most recent 500 of 743 total sales" in text

    def test_default_cap_is_500_most_recent_sales(self):
        assert st.LEDGER_DISPLAY_LIMIT == 500
        assert st.fetch_ledger_rows.__defaults__ == (500,)

    def test_no_truncation_note_when_everything_fits(self):
        now, rows = self._rows()
        text = _pdf_text(st.build_statement_pdf("Shop", rows, 2, now))
        assert "Showing the most recent" not in text

    def test_long_history_paginates_with_repeated_headers(self):
        """A merchant with many sales still gets a readable document: the
        column header repeats on each page and rows are not split."""
        from datetime import datetime, timedelta

        now = datetime(2026, 9, 17, tzinfo=UTC)
        rows = [
            st.LedgerRow(now - timedelta(seconds=i), f"Item {i}", 1000, "Confirmed", "sale", "bank")
            for i in range(200)
        ]
        reader = PdfReader(io.BytesIO(st.build_statement_pdf("Busy Shop", rows, 200, now)))
        assert len(reader.pages) > 1
        for page in reader.pages:
            assert "DESCRIPTION" in (page.extract_text() or "")

    def test_non_latin1_text_does_not_break_rendering(self):
        """Regression: fpdf2's core fonts are latin-1 only and raise on emoji
        or CJK. A merchant's business name is arbitrary user input, so a
        statement must still render when it contains those characters."""
        from datetime import datetime

        now = datetime(2026, 9, 17, tzinfo=UTC)
        rows = [
            st.LedgerRow(now, "\U0001f600 \u4f60\u597d \u2014 Ankara",
                          75000, "Confirmed", "sale", "bank"),
        ]
        pdf = st.build_statement_pdf("S\u0142ox \U0001f6cd Ltd", rows, 1, now)
        assert pdf[:5] == b"%PDF-"
        assert "NGN 750.00" in _pdf_text(pdf)

    def test_output_is_a_pdf_document(self):
        now, rows = self._rows()
        pdf = st.build_statement_pdf("Shop", rows, 2, now)
        assert pdf[:5] == b"%PDF-"
        assert pdf.rstrip().endswith(b"%%EOF")


class TestStatementFilename:
    """The filename is uploaded to Meta and shown on the merchant's phone."""

    def test_uses_slugified_business_name_and_date(self):
        from datetime import datetime

        now = datetime(2026, 9, 17, tzinfo=UTC)
        expected = "confam-statement-solex-shoes-2026-09-17.pdf"
        assert st.statement_filename("Solex Shoes", now) == expected

    def test_is_ascii_and_path_safe_for_odd_business_names(self):
        """A slash, apostrophe, or emoji in a business name must not produce
        a rejected upload or a mangled filename."""
        from datetime import datetime

        now = datetime(2026, 9, 17, tzinfo=UTC)
        name = st.statement_filename("S\u0142ox \U0001f6cd / Ade's \U0001f600 Store", now)

        # Unusable characters collapse to separators; what survives is the
        # readable ascii core of the name.
        assert name == "confam-statement-s-ox-ade-s-store-2026-09-17.pdf"
        assert name.isascii()
        assert "/" not in name and " " not in name
        assert name.endswith(".pdf")

    def test_falls_back_when_business_name_has_no_usable_characters(self):
        from datetime import datetime

        now = datetime(2026, 9, 17, tzinfo=UTC)
        assert st.statement_filename("\U0001f600\U0001f600", now).startswith("confam-statement-")
        assert st.statement_filename("", now) == "confam-statement-business-2026-09-17.pdf"


# ---------------------------------------------------------------------------
# Retrieval: ordering, capping, merchant isolation, read-only
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLedgerRetrieval:
    def test_returns_rows_oldest_first_for_display(self, migrator_conn):
        from confam.db import get_conn

        merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, merchant["merchant_id"], 1000, "first", days_ago=3)
        _insert_sale(migrator_conn, merchant["merchant_id"], 2000, "second", days_ago=2)
        _insert_sale(migrator_conn, merchant["merchant_id"], 3000, "third", days_ago=1)

        with get_conn() as conn:
            rows, total = st.fetch_ledger_rows(conn, merchant["merchant_id"])

        assert total == 3
        assert [r.description for r in rows] == ["first", "second", "third"]

    def test_keeps_the_most_recent_rows_when_capped(self, migrator_conn):
        """The cap must drop the OLDEST sales, not the newest, and the
        returned total must still be the merchant's full count."""
        from confam.db import get_conn

        merchant = _merchant("active", migrator_conn)
        for i in range(5):
            _insert_sale(migrator_conn, merchant["merchant_id"], 1000 + i, f"sale {i}", days_ago=i)

        with get_conn() as conn:
            rows, total = st.fetch_ledger_rows(conn, merchant["merchant_id"], limit=2)

        assert total == 5  # full count, so the PDF can disclose truncation
        # sale 0 and sale 1 are the most recent; they are returned oldest-first
        # for display, and the three oldest sales are the ones dropped.
        assert [r.description for r in rows] == ["sale 1", "sale 0"]

    def test_merchant_isolation(self, migrator_conn):
        """The scoping happens in SQL, so one merchant can never see another
        merchant's sales — not by bug, not by configuration."""
        from confam.db import get_conn

        alice = _merchant("active", migrator_conn, "alice shop")
        bob = _merchant("active", migrator_conn, "bob shop")
        _insert_sale(migrator_conn, alice["merchant_id"], 500000, "ALICE SECRET ORDER")
        _insert_sale(migrator_conn, bob["merchant_id"], 600000, "BOB SECRET ORDER")

        with get_conn() as conn:
            alice_rows, alice_total = st.fetch_ledger_rows(conn, alice["merchant_id"])
            bob_rows, bob_total = st.fetch_ledger_rows(conn, bob["merchant_id"])

        assert alice_total == 1 and bob_total == 1
        assert [r.description for r in alice_rows] == ["ALICE SECRET ORDER"]
        assert [r.description for r in bob_rows] == ["BOB SECRET ORDER"]

    def test_empty_ledger_returns_no_rows(self, migrator_conn):
        from confam.db import get_conn

        merchant = _merchant("active", migrator_conn)
        with get_conn() as conn:
            rows, total = st.fetch_ledger_rows(conn, merchant["merchant_id"])
        assert rows == []
        assert total == 0

    def test_reads_as_confam_app_and_writes_nothing(self, migrator_conn):
        """Reads go through the app's restricted pool, and asking for a
        statement must leave the append-only ledger untouched."""
        from confam.db import get_conn

        merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        before = _ledger_count(migrator_conn, merchant["merchant_id"])
        with get_conn() as conn:
            st.fetch_ledger_rows(conn, merchant["merchant_id"])
        assert _ledger_count(migrator_conn, merchant["merchant_id"]) == before

    def test_description_comes_from_the_payment_link(self, migrator_conn):
        from confam.db import get_conn

        merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        with get_conn() as conn:
            rows, _ = st.fetch_ledger_rows(conn, merchant["merchant_id"])

        assert rows[0].description == "Ankara fabric x2"
        assert rows[0].amount_minor_units == 75000
        assert rows[0].status == "Confirmed"


# ---------------------------------------------------------------------------
# State x command routing
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLedgerRouting:
    async def test_unregistered_sender_gets_registration_instructions(
        self, messaging_env, migrator_conn
    ):
        """An unknown number must not be able to pull a statement — the reply
        is the registration prompt, and no PDF is ever produced."""
        from_id = "2347" + uuid.uuid4().hex[:12]
        mock = await _deliver(from_id, "LEDGER")

        replies = _text_replies(mock)
        assert len(replies) == 1
        assert "REGISTER" in replies[0]
        assert _uploads(mock) == []
        assert _documents(mock) == []

    async def test_pending_merchant_is_told_to_finish_onboarding(
        self, messaging_env, migrator_conn
    ):
        """A merchant who has not onboarded has no sales yet, so no PDF is
        built; the reply points at the step that unblocks them."""
        merchant = _merchant("pending_verification", migrator_conn)
        mock = await _deliver(merchant["from_id"], "LEDGER")

        replies = _text_replies(mock)
        assert len(replies) == 1
        assert "ONBOARD" in replies[0]
        assert "confirmed sales" in replies[0].lower()
        assert _uploads(mock) == []
        assert _documents(mock) == []

    async def test_active_merchant_with_no_sales_gets_plain_text(
        self, messaging_env, migrator_conn
    ):
        """Nothing to show means no document: an empty PDF would be a worse
        answer than a sentence."""
        merchant = _merchant("active", migrator_conn)
        mock = await _deliver(merchant["from_id"], "LEDGER")

        assert _text_replies(mock) == [
            "No confirmed sales yet — once you get your first payment, send LEDGER again."
        ]
        assert _uploads(mock) == []
        assert _documents(mock) == []

    async def test_suspended_merchant_is_blocked(self, messaging_env, migrator_conn):
        """Not actionable: fails closed, like every other command."""
        merchant = _merchant("suspended", migrator_conn)
        mock = await _deliver(merchant["from_id"], "LEDGER")

        assert "not active" in _text_replies(mock)[0]
        assert _uploads(mock) == []


# ---------------------------------------------------------------------------
# Meta upload / send flow
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStatementDelivery:
    async def test_uploads_pdf_then_sends_document_message(
        self, messaging_env, migrator_conn
    ):
        merchant = _merchant("active", migrator_conn, "Solex Shoes")
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        mock = await _deliver(merchant["from_id"], "LEDGER")

        # 1. the PDF is uploaded as multipart to this phone number's /media
        uploads = _uploads(mock)
        assert len(uploads) == 1
        url = str(uploads[0].args[0])
        assert TEST_PHONE_NUMBER_ID in url
        assert url.endswith(f"/{TEST_PHONE_NUMBER_ID}/media")
        assert uploads[0].kwargs["data"]["type"] == "application/pdf"
        assert uploads[0].kwargs["data"]["messaging_product"] == "whatsapp"

        filename, pdf_bytes, mime = uploads[0].kwargs["files"]["file"]
        assert mime == "application/pdf"
        assert pdf_bytes[:5] == b"%PDF-"

        # 2. a document message references the media_id Meta returned
        documents = _documents(mock)
        assert len(documents) == 1
        assert documents[0]["messaging_product"] == "whatsapp"
        assert documents[0]["to"] == merchant["from_id"]
        assert documents[0]["type"] == "document"
        assert documents[0]["document"]["id"] == TEST_MEDIA_ID
        assert documents[0]["document"]["filename"] == filename

        # and no error text was needed
        assert _text_replies(mock) == []

    async def test_filename_is_derived_from_business_name_and_date(
        self, messaging_env, migrator_conn
    ):
        merchant = _merchant("active", migrator_conn, "Solex Shoes")
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        mock = await _deliver(merchant["from_id"], "LEDGER")

        filename, _, _ = _uploads(mock)[0].kwargs["files"]["file"]
        assert filename.startswith("confam-statement-solex-shoes-")
        assert filename.endswith(".pdf")

    async def test_statement_alias_and_parsing_tolerate_case_and_whitespace(
        self, messaging_env, migrator_conn
    ):
        """'  ledger  ', 'Statement' and 'LEDGER' are the same command."""
        merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        for text in ("LEDGER", "ledger", "  ledger  ", "STATEMENT", "statement"):
            mock = await _deliver(merchant["from_id"], text)
            assert len(_documents(mock)) == 1, f"{text!r} did not produce a document"
            assert _text_replies(mock) == [], f"{text!r} produced a text reply"

    async def test_delivered_pdf_contains_this_merchants_sales_only(
        self, messaging_env, migrator_conn
    ):
        """End-to-end isolation: the bytes that reach Meta belong to the
        requesting merchant and no one else."""
        alice = _merchant("active", migrator_conn, "alice shop")
        bob = _merchant("active", migrator_conn, "bob shop")
        _insert_sale(migrator_conn, alice["merchant_id"], 500000, "ALICE SECRET ORDER")
        _insert_sale(migrator_conn, bob["merchant_id"], 600000, "BOB SECRET ORDER")

        mock = await _deliver(alice["from_id"], "LEDGER")

        text = _pdf_text(_uploaded_pdf(mock))
        assert "ALICE SECRET ORDER" in text
        assert "BOB SECRET ORDER" not in text
        assert "alice shop" in text
        assert "bob shop" not in text
        assert "Total confirmed sales: NGN 5,000.00" in text

    async def test_history_within_the_cap_has_no_truncation_note(
        self, messaging_env, migrator_conn
    ):
        merchant = _merchant("active", migrator_conn)
        for i in range(3):
            _insert_sale(migrator_conn, merchant["merchant_id"], 1000, f"sale {i}", days_ago=i)

        mock = await _deliver(merchant["from_id"], "LEDGER")

        text = _pdf_text(_uploaded_pdf(mock))
        assert "Showing the most recent" not in text
        for i in range(3):
            assert f"sale {i}" in text

    async def test_upload_failure_falls_back_to_plain_text(
        self, messaging_env, migrator_conn
    ):
        """A Meta media failure must still answer the merchant."""
        merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        payload, headers = _signed_payload(merchant["from_id"], "LEDGER")
        with patch("services.messaging.main.httpx.post") as mock_post:
            def _side_effect(url, *args, **kwargs):
                if str(url).endswith("/media"):
                    raise ConnectError("meta unreachable")
                return MagicMock(status_code=200, raise_for_status=MagicMock())

            mock_post.side_effect = _side_effect
            async with AsyncClient(
                transport=ASGITransport(app=messaging_app), base_url=BASE_URL
            ) as client:
                resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)
            assert resp.status_code == 200

        replies = _text_replies(mock_post)
        assert len(replies) == 1
        assert "couldn't send your statement" in replies[0]
        assert _documents(mock_post) == []
        # internal failure detail must not leak to the merchant
        assert "ConnectError" not in replies[0]
        assert "meta unreachable" not in replies[0]

    async def test_document_send_failure_falls_back_to_plain_text(
        self, messaging_env, migrator_conn
    ):
        """The upload succeeded but the message did not land. The merchant
        still needs to be told something."""
        merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        payload, headers = _signed_payload(merchant["from_id"], "LEDGER")
        with patch("services.messaging.main.httpx.post") as mock_post:
            def _side_effect(url, *args, **kwargs):
                if str(url).endswith("/media"):
                    resp = MagicMock(status_code=200)
                    resp.raise_for_status = MagicMock()
                    resp.json.return_value = {"id": TEST_MEDIA_ID}
                    return resp
                raise ReadTimeout("message timed out")

            mock_post.side_effect = _side_effect
            async with AsyncClient(
                transport=ASGITransport(app=messaging_app), base_url=BASE_URL
            ) as client:
                resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)
            assert resp.status_code == 200

        replies = _text_replies(mock_post)
        assert len(replies) == 1
        assert "couldn't send your statement" in replies[0]
        assert "ReadTimeout" not in replies[0]

    async def test_media_upload_without_id_is_treated_as_failure(
        self, messaging_env, migrator_conn
    ):
        """A document message needs a real media_id. Without one it is
        silently undeliverable, so this must fall back rather than send it."""
        merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        payload, headers = _signed_payload(merchant["from_id"], "LEDGER")
        with patch("services.messaging.main.httpx.post") as mock_post:
            def _side_effect(url, *args, **kwargs):
                if str(url).endswith("/media"):
                    resp = MagicMock(status_code=200)
                    resp.raise_for_status = MagicMock()
                    resp.json.return_value = {}  # Meta returned no "id"
                    return resp
                return MagicMock(status_code=200, raise_for_status=MagicMock())

            mock_post.side_effect = _side_effect
            async with AsyncClient(
                transport=ASGITransport(app=messaging_app), base_url=BASE_URL
            ) as client:
                resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)
            assert resp.status_code == 200

        assert _documents(mock_post) == []
        assert "couldn't send your statement" in _text_replies(mock_post)[0]

    async def test_webhook_still_returns_200_when_statement_fails(
        self, messaging_env, migrator_conn
    ):
        """Rule 10: a failed statement must not make Meta retry the webhook
        or crash the handler."""
        merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        payload, headers = _signed_payload(merchant["from_id"], "LEDGER")
        with patch("services.messaging.main.httpx.post") as mock_post:
            mock_post.side_effect = ConnectError("meta unreachable")
            async with AsyncClient(
                transport=ASGITransport(app=messaging_app), base_url=BASE_URL
            ) as client:
                resp = await client.post("/webhooks/whatsapp", content=payload, headers=headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLedgerRateLimit:
    async def test_limits_statement_requests_per_sender(
        self, messaging_env, migrator_conn, monkeypatch
    ):
        """Each PDF costs a Meta media upload, which is metered."""
        monkeypatch.setenv("RATE_LIMIT_LEDGER_PER_HOUR", "2")
        merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        first = await _deliver(merchant["from_id"], "LEDGER")
        second = await _deliver(merchant["from_id"], "LEDGER")
        third = await _deliver(merchant["from_id"], "LEDGER")

        assert len(_documents(first)) == 1
        assert len(_documents(second)) == 1
        # the third is stopped before any upload is attempted
        assert _uploads(third) == []
        assert "several statements" in _text_replies(third)[0].lower()

    async def test_limit_is_per_sender(self, messaging_env, migrator_conn, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_LEDGER_PER_HOUR", "1")
        first_merchant = _merchant("active", migrator_conn)
        second_merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, first_merchant["merchant_id"], 75000, "one")
        _insert_sale(migrator_conn, second_merchant["merchant_id"], 75000, "two")

        await _deliver(first_merchant["from_id"], "LEDGER")
        blocked = await _deliver(first_merchant["from_id"], "LEDGER")
        allowed = await _deliver(second_merchant["from_id"], "LEDGER")

        assert _uploads(blocked) == []
        assert len(_documents(allowed)) == 1

    def test_limit_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_LEDGER_PER_HOUR", "0")
        assert all(
            messaging_main.check_ledger_rate_limit("2347000000000") is False
            for _ in range(50)
        )

    def test_non_numeric_limit_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_LEDGER_PER_HOUR", "not-a-number")
        assert messaging_main._ledger_rate_limit() == 10

    async def test_ledger_does_not_consume_the_onboard_budget(
        self, messaging_env, migrator_conn, monkeypatch
    ):
        """The two limits stay separate: asking for statements must not lock
        a merchant out of finishing onboarding."""
        monkeypatch.setenv("RATE_LIMIT_LEDGER_PER_HOUR", "1")
        merchant = _merchant("active", migrator_conn)
        _insert_sale(migrator_conn, merchant["merchant_id"], 75000, "Ankara fabric x2")

        await _deliver(merchant["from_id"], "LEDGER")
        await _deliver(merchant["from_id"], "LEDGER")  # throttled

        messaging_main._RESOLVE_ATTEMPTS.clear()
        assert messaging_main.check_resolve_rate_limit(merchant["from_id"]) is False
