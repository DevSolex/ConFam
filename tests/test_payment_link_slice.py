"""
Tests for the vertical slice: create payment link + render checkout.

All tests run against the real confam_app role (TEST_DATABASE_URL).
This is the first proof that OQ-018 grants are sufficient for real
application behaviour, not just for the privilege-rejection test.

Coverage:
  - Creating a link with valid input persists it and returns the right URL shape.
  - Creating a link with invalid input is rejected at the application layer.
  - Opening a valid unexpired link transitions created → opened.
  - Opening an expired link returns 410, does not render transaction details.
  - Opening an already-opened link a second time is idempotent.
  - All DB operations use confam_app (no superuser privileges in the request path).
"""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest
from httpx import ASGITransport, AsyncClient

from confam.links import (
    LinkValidationError,
    PaymentLink,
    _validate_create_inputs,
    create_link,
    get_link,
    open_link,
)
from services.checkout.main import app as checkout_app
from services.settlement_engine.main import app as engine_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_conn():
    """
    Direct psycopg2 connection as confam_app (function-scoped).
    Each test gets a fresh connection so an aborted transaction in one test
    never bleeds into the next.
    Proves OQ-018 grants are sufficient for real application behaviour.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture()
def migrator_conn():
    """
    Separate psycopg2 connection as confam_migrator for test fixture cleanup.
    confam_app has no DELETE on any table (by design — Rule 1/2), so test
    teardown that needs to remove seed data must use the migrator role.
    This connection is separate from db_conn so fixture cleanup never touches
    the application-role connection's transaction state.
    """
    url = os.environ.get("ALEMBIC_DATABASE_URL", "").replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    if not url:
        pytest.skip("ALEMBIC_DATABASE_URL not set")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture()
def test_merchant_id(db_conn, migrator_conn) -> Iterator[str]:
    """
    Insert a minimal merchant row via confam_app and return its merchant_id.
    Teardown removes the test data via migrator_conn (confam_app has no DELETE).
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO merchants (whatsapp_number, confam_thread_id, status)
            VALUES (%s, %s, 'active')
            RETURNING merchant_id
            """,
            ("+234-000-TEST-SLICE1", f"test-thread-{uuid.uuid4()}"),
        )
        merchant_id = str(cur.fetchone()[0])
    db_conn.commit()
    yield merchant_id
    # Cleanup via migrator role — confam_app has no DELETE.
    with migrator_conn.cursor() as cur:
        cur.execute("DELETE FROM payment_links WHERE merchant_id = %s", (merchant_id,))
        cur.execute("DELETE FROM merchants WHERE merchant_id = %s", (merchant_id,))
    migrator_conn.commit()


@pytest.fixture()
def valid_link(db_conn, test_merchant_id) -> PaymentLink:
    """A freshly-created valid payment link for use in checkout tests."""
    link = create_link(
        conn=db_conn,
        merchant_id=test_merchant_id,
        amount_minor_units=50000,  # ₦500.00
        currency="NGN",
        description="Test item for vertical slice",
    )
    return link


@pytest.fixture()
async def engine_client():
    """Async HTTPX test client for the settlement-engine service."""
    async with AsyncClient(
        transport=ASGITransport(app=engine_app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture()
async def checkout_client():
    """Async HTTPX test client for the checkout service."""
    async with AsyncClient(
        transport=ASGITransport(app=checkout_app), base_url="http://test"
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Input validation tests (pure — no DB required)
# ---------------------------------------------------------------------------

class TestValidateCreateInputs:
    """Application-layer validation fires before any DB interaction."""

    def test_rejects_empty_merchant_id(self):
        with pytest.raises(LinkValidationError, match="merchant_id is required"):
            _validate_create_inputs("", 1000, "NGN", "item")

    def test_rejects_invalid_merchant_uuid(self):
        with pytest.raises(LinkValidationError, match="valid UUID"):
            _validate_create_inputs("not-a-uuid", 1000, "NGN", "item")

    def test_rejects_zero_amount(self):
        with pytest.raises(LinkValidationError, match="positive"):
            _validate_create_inputs(str(uuid.uuid4()), 0, "NGN", "item")

    def test_rejects_negative_amount(self):
        with pytest.raises(LinkValidationError, match="positive"):
            _validate_create_inputs(str(uuid.uuid4()), -500, "NGN", "item")

    def test_rejects_float_amount(self):
        # Rule 6: floats must be caught at the application layer, not DB layer.
        with pytest.raises(LinkValidationError, match="integer"):
            _validate_create_inputs(str(uuid.uuid4()), 10.50, "NGN", "item")  # type: ignore[arg-type]

    def test_rejects_unsupported_currency(self):
        with pytest.raises(LinkValidationError, match="not supported"):
            _validate_create_inputs(str(uuid.uuid4()), 1000, "USD", "item")

    def test_rejects_empty_description(self):
        with pytest.raises(LinkValidationError, match="description is required"):
            _validate_create_inputs(str(uuid.uuid4()), 1000, "NGN", "")

    def test_rejects_description_over_500_chars(self):
        with pytest.raises(LinkValidationError, match="500 characters"):
            _validate_create_inputs(str(uuid.uuid4()), 1000, "NGN", "x" * 501)

    def test_accepts_valid_inputs(self):
        # Should not raise.
        _validate_create_inputs(str(uuid.uuid4()), 1, "NGN", "a")


# ---------------------------------------------------------------------------
# Domain layer tests — confam_app role, direct DB
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestCreateLink:
    """confam.links.create_link() — runs as confam_app."""

    def test_persists_link_with_correct_fields(self, db_conn, test_merchant_id):
        link = create_link(db_conn, test_merchant_id, 10000, "NGN", "Bag of rice")
        assert link.merchant_id == test_merchant_id
        assert link.amount_minor_units == 10000
        assert isinstance(link.amount_minor_units, int)  # Rule 6
        assert link.currency == "NGN"
        assert link.description == "Bag of rice"
        assert link.status == "created"
        assert link.expires_at > datetime.now(UTC)

    def test_link_id_is_valid_uuid(self, db_conn, test_merchant_id):
        link = create_link(db_conn, test_merchant_id, 500, "NGN", "item")
        uuid.UUID(link.link_id)  # raises ValueError if not a valid UUID

    def test_expiry_is_approximately_30_minutes(self, db_conn, test_merchant_id):
        before = datetime.now(UTC)
        link = create_link(db_conn, test_merchant_id, 500, "NGN", "item")
        after = datetime.now(UTC)
        lower = before + timedelta(seconds=1799)
        upper = after + timedelta(seconds=1801)
        assert lower <= link.expires_at <= upper

    def test_two_links_have_different_ids(self, db_conn, test_merchant_id):
        a = create_link(db_conn, test_merchant_id, 1000, "NGN", "item a")
        b = create_link(db_conn, test_merchant_id, 2000, "NGN", "item b")
        assert a.link_id != b.link_id

    def test_rejects_unknown_merchant(self, db_conn):
        """confam_app FK constraint rejects a non-existent merchant_id."""
        with pytest.raises(Exception):  # ForeignKeyViolation
            create_link(db_conn, str(uuid.uuid4()), 1000, "NGN", "item")
        # Always rollback after an expected exception so the connection is clean
        # for subsequent tests that share this session-scoped connection.
        db_conn.rollback()


@pytest.mark.integration
class TestOpenLink:
    """confam.links.open_link() — runs as confam_app."""

    def test_transitions_created_to_opened(self, db_conn, valid_link):
        opened = open_link(db_conn, valid_link.link_id)
        assert opened is not None
        assert opened.status == "opened"

    def test_idempotent_on_already_opened(self, db_conn, valid_link):
        """Opening twice must not error and must not change any other field."""
        first = open_link(db_conn, valid_link.link_id)
        second = open_link(db_conn, valid_link.link_id)
        assert first is not None
        assert second is not None
        assert second.status == "opened"
        assert second.amount_minor_units == first.amount_minor_units

    def test_returns_none_for_nonexistent_link(self, db_conn):
        result = open_link(db_conn, str(uuid.uuid4()))
        assert result is None

    def test_returns_none_for_invalid_uuid(self, db_conn):
        result = open_link(db_conn, "not-a-uuid")
        assert result is None

    def test_does_not_open_expired_link(self, db_conn, test_merchant_id):
        """
        A link whose expires_at is in the past must not be transitioned to
        'opened' — the conditional UPDATE will not fire.
        """
        # Insert a link directly with an already-expired expires_at.
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payment_links
                    (merchant_id, amount_minor_units, currency, description,
                     status, expires_at)
                VALUES (%s, 1000, 'NGN', 'expired item', 'created',
                        now() - interval '1 second')
                RETURNING link_id
                """,
                (test_merchant_id,),
            )
            link_id = str(cur.fetchone()[0])
        db_conn.commit()

        result = open_link(db_conn, link_id)
        # Link exists but status was NOT transitioned.
        assert result is not None
        assert result.status == "created"  # still 'created', not 'opened'
        assert result.is_expired is True


# ---------------------------------------------------------------------------
# HTTP endpoint tests — settlement-engine service
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestCreateLinkEndpoint:
    """POST /links via the settlement-engine ASGI app, confam_app role."""

    @pytest.mark.asyncio
    async def test_returns_201_and_checkout_url(
        self, engine_client, test_merchant_id, monkeypatch
    ):
        monkeypatch.setenv("CHECKOUT_BASE_URL", "http://pay.confam.co")
        resp = await engine_client.post("/links", json={
            "merchant_id": test_merchant_id,
            "amount_minor_units": 25000,
            "currency": "NGN",
            "description": "Ankara fabric",
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["checkout_url"].startswith("http://pay.confam.co/")
        assert body["status"] == "created"
        uuid.UUID(body["link_id"])  # valid UUID

    @pytest.mark.asyncio
    async def test_rejects_negative_amount_with_422(
        self, engine_client, test_merchant_id
    ):
        resp = await engine_client.post("/links", json={
            "merchant_id": test_merchant_id,
            "amount_minor_units": -1,
            "currency": "NGN",
            "description": "item",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_rejects_float_amount_with_422(
        self, engine_client, test_merchant_id
    ):
        # Rule 6: floats must be rejected before any DB write.
        resp = await engine_client.post("/links", json={
            "merchant_id": test_merchant_id,
            "amount_minor_units": 100.50,
            "currency": "NGN",
            "description": "item",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_rejects_missing_description_with_422(
        self, engine_client, test_merchant_id
    ):
        resp = await engine_client.post("/links", json={
            "merchant_id": test_merchant_id,
            "amount_minor_units": 1000,
            "currency": "NGN",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_rejects_unknown_merchant_with_404(self, engine_client):
        resp = await engine_client.post("/links", json={
            "merchant_id": str(uuid.uuid4()),
            "amount_minor_units": 1000,
            "currency": "NGN",
            "description": "item",
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# HTTP endpoint tests — checkout service
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestCheckoutEndpoint:
    """GET /{link_id}/json via the checkout ASGI app, confam_app role."""

    @pytest.mark.asyncio
    async def test_valid_link_returns_200_and_transitions_to_opened(
        self, checkout_client, valid_link, db_conn
    ):
        resp = await checkout_client.get(f"/{valid_link.link_id}/json")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "opened"
        assert body["description"] == valid_link.description
        assert body["amount_minor_units"] == valid_link.amount_minor_units
        # Confirm the transition persisted in the DB.
        db_link = get_link(db_conn, valid_link.link_id)
        assert db_link is not None
        assert db_link.status == "opened"

    @pytest.mark.asyncio
    async def test_opening_twice_is_idempotent(
        self, checkout_client, valid_link
    ):
        first = await checkout_client.get(f"/{valid_link.link_id}/json")
        second = await checkout_client.get(f"/{valid_link.link_id}/json")
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["amount_minor_units"] == second.json()["amount_minor_units"]

    @pytest.mark.asyncio
    async def test_nonexistent_link_returns_404(self, checkout_client):
        resp = await checkout_client.get(f"/{uuid.uuid4()}/json")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_expired_link_returns_410(
        self, checkout_client, db_conn, test_merchant_id
    ):
        # Insert an expired link directly.
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payment_links
                    (merchant_id, amount_minor_units, currency, description,
                     status, expires_at)
                VALUES (%s, 1000, 'NGN', 'expired', 'created',
                        now() - interval '1 second')
                RETURNING link_id
                """,
                (test_merchant_id,),
            )
            link_id = str(cur.fetchone()[0])
        db_conn.commit()

        resp = await checkout_client.get(f"/{link_id}/json")
        assert resp.status_code == 410
        assert "expired" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_already_paid_link_returns_410(
        self, checkout_client, db_conn, test_merchant_id
    ):
        # Insert a link in 'paid' status (simulates a completed payment).
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payment_links
                    (merchant_id, amount_minor_units, currency, description,
                     status, expires_at)
                VALUES (%s, 1000, 'NGN', 'paid item', 'paid',
                        now() + interval '30 minutes')
                RETURNING link_id
                """,
                (test_merchant_id,),
            )
            link_id = str(cur.fetchone()[0])
        db_conn.commit()

        resp = await checkout_client.get(f"/{link_id}/json")
        assert resp.status_code == 410
